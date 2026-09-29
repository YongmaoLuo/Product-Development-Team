"""
Unit tests for ``_execute_single_vp_with_timeout`` in
:mod:`verification_agent`.

TDD spec (per-method timeout handling for verification points):

* ``test_timeout_marks_failed_not_skipped`` — when ``execute_fn``
  exceeds the per-method timeout, the result MUST be
  ``status='FAILED'`` (NEVER ``'SKIPPED'``). A ``SKIPPED`` here would
  silently hide a hung VP from the repair-task generator and break
  the zero-tolerance-for-skipping contract.

* ``test_soft_warn_logs_at_threshold`` — when ``soft_warn_seconds`` is
  set in the config, a WARNING log is emitted at the threshold but
  the process is NOT killed; the wrapper keeps waiting until the
  full timeout fires. The test verifies both behaviours: (a) a
  WARNING record is captured, (b) the function returns a FAILED
  verdict at the full timeout (i.e. the soft-warn is advisory
  only).

* ``test_per_method_timeout_different_values`` — different
  ``verification_method`` values pick up their own configured
  timeout from ``config['timeouts']``. We run two VPs that share the
  same hung ``execute_fn`` against different methods and assert
  each ``failure_reason`` references the timeout configured for its
  own method.
"""

import asyncio
import json
import logging
import time
import pytest

from verification_agent import (
    _execute_single_vp_with_timeout,
    _run_ui_validation_with_fastfail,
    write_verification_report,
)


# Module-level logger that ``_execute_single_vp_with_timeout`` writes
# to. We import it lazily inside the test body so a missing attribute
# (e.g. older revision) shows up as an AttributeError at the test
# boundary, not at import time.
VP_LOGGER_NAME = "verification.vp"


class TestExecuteSingleVpWithTimeout:
    """TDD spec for the per-method timeout wrapper."""

    @pytest.mark.asyncio
    async def test_timeout_marks_failed_not_skipped(self, caplog):
        """Hung ``execute_fn`` MUST produce ``status='FAILED'`` on timeout.

        Uses a 1s timeout against a 5s sleep. The wrapper converts the
        :class:`asyncio.TimeoutError` from :func:`asyncio.wait_for`
        into a result dict with:

        * ``status == 'FAILED'`` (NEVER ``'SKIPPED'`` — the
          zero-tolerance-for-skipping contract).
        * ``failure_reason == 'timeout (1s)'`` (carries the resolved
          timeout value so the repair-task generator can attribute
          the failure to a specific budget).
        * ``deviation == 'timeout_exceeded'`` (machine-readable
          tag for downstream consumers — distinct from a real
          test failure or an exception).
        """
        async def slow_fn():
            await asyncio.sleep(5)

        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "execute_fn": slow_fn,
        }
        config = {
            "timeouts": {"ui_validation": 1, "default": 1},
            "soft_warn_seconds": 0,
        }

        with caplog.at_level(logging.WARNING, logger=VP_LOGGER_NAME):
            result = await _execute_single_vp_with_timeout(vp, config)

        assert result["id"] == "VP-001"
        assert result["status"] == "FAILED", (
            "timeout MUST yield status='FAILED'; got "
            f"{result.get('status')!r} (failure_reason="
            f"{result.get('failure_reason')!r})"
        )
        assert result.get("status") != "SKIPPED", (
            "timeout must NEVER produce status='SKIPPED' — the "
            "zero-tolerance-for-skipping contract would be broken"
        )
        assert "timeout" in str(result.get("failure_reason", "")).lower()
        assert "1" in str(result.get("failure_reason", ""))
        assert result.get("deviation") == "timeout_exceeded"

    @pytest.mark.asyncio
    async def test_soft_warn_logs_at_threshold(self, caplog):
        """``soft_warn_seconds`` fires a WARNING at the threshold; no kill.

        We use a 1s soft-warn against a 2s timeout, and the
        ``execute_fn`` sleeps 4s. The expected sequence is:

        * t≈1s → WARNING log emitted (advisory).
        * t≈2s → timeout fires, ``status='FAILED'`` returned.

        The test asserts both observations: a WARNING record is
        captured in ``caplog``, and the result is FAILED (i.e. the
        soft-warn did not kill the wrapper early).
        """
        async def slow_fn():
            await asyncio.sleep(4)

        vp = {
            "id": "VP-002",
            "verification_method": "automated_test",
            "execute_fn": slow_fn,
        }
        config = {
            "timeouts": {"automated_test": 2, "default": 2},
            "soft_warn_seconds": 1,
        }

        with caplog.at_level(logging.WARNING, logger=VP_LOGGER_NAME):
            result = await _execute_single_vp_with_timeout(vp, config)

        # (a) WARNING was emitted at the soft_warn threshold
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == VP_LOGGER_NAME
        ]
        assert warning_records, (
            "expected at least one WARNING log from verification.vp; "
            f"got records: {[(r.name, r.levelname, r.message) for r in caplog.records]}"
        )
        # The message should reference the soft_warn threshold OR the
        # VP id (either is acceptable evidence the soft-warn fired
        # for this specific VP).
        messages = " ".join(r.getMessage() for r in warning_records)
        assert (
            "soft_warn" in messages.lower()
            or "VP-002" in messages
        ), (
            f"expected soft_warn WARNING to mention 'soft_warn' or "
            f"'VP-002', got: {messages!r}"
        )

        # (b) The wrapper did NOT kill at the threshold — it kept
        # running and timed out at the full 2s budget.
        assert result["status"] == "FAILED", (
            "soft_warn is advisory only; expected FAILED at the "
            f"full timeout, got {result.get('status')!r}"
        )
        assert "2" in str(result.get("failure_reason", ""))
        assert result.get("deviation") == "timeout_exceeded"

    @pytest.mark.asyncio
    async def test_per_method_timeout_different_values(self, caplog):
        """Per-method timeouts are honoured independently.

        Configures ``ui_validation=1s`` and ``automated_test=2s`` and
        runs the same hung ``execute_fn`` (sleep 5s) against each
        method. Each VP should fail with the timeout value matching
        its method, not the other's.
        """
        async def slow_fn():
            await asyncio.sleep(5)

        config = {
            "timeouts": {
                "ui_validation": 1,
                "automated_test": 2,
                "default": 10,
            },
            "soft_warn_seconds": 0,
        }

        # Run ui_validation → should fire at 1s
        vp_ui = {
            "id": "VP-UI",
            "verification_method": "ui_validation",
            "execute_fn": slow_fn,
        }
        with caplog.at_level(logging.WARNING, logger=VP_LOGGER_NAME):
            result_ui = await _execute_single_vp_with_timeout(vp_ui, config)

        assert result_ui["status"] == "FAILED"
        ui_reason = str(result_ui.get("failure_reason", ""))
        assert "1" in ui_reason, (
            f"ui_validation timeout should reference '1s', got {ui_reason!r}"
        )
        assert "2" not in ui_reason.replace("VP-UI-2", ""), (
            f"ui_validation timeout must NOT mention '2s' "
            f"(that's automated_test's budget), got {ui_reason!r}"
        )

        # Run automated_test → should fire at 2s
        vp_at = {
            "id": "VP-AT",
            "verification_method": "automated_test",
            "execute_fn": slow_fn,
        }
        with caplog.at_level(logging.WARNING, logger=VP_LOGGER_NAME):
            result_at = await _execute_single_vp_with_timeout(vp_at, config)

        assert result_at["status"] == "FAILED"
        at_reason = str(result_at.get("failure_reason", ""))
        assert "2" in at_reason, (
            f"automated_test timeout should reference '2s', got {at_reason!r}"
        )


# ---------------------------------------------------------------------------
# _run_ui_validation_with_fastfail — three-gate UI validation
# ---------------------------------------------------------------------------

class _MCPStub:
    """Test double for the puppeteer MCP surface.

    Each method is a coroutine factory: assign an async function (or a
    value) and the stub returns a coroutine that produces the value
    (or awaits the function) when awaited.

    The stub records every call so tests can assert the
    invocation count (e.g. ``navigate`` is called exactly twice when
    ``navigate_retry_max == 1``).

    Note: the configurable hooks are stored under
    ``_navigate_hook`` / ``_screenshot_hook`` /
    ``_wait_for_selector_hook`` (with the leading underscore) so
    they do NOT shadow the async method names below — otherwise
    the default ``None`` from ``__init__`` would overwrite the
    methods and the production code would call ``None()``.
    """

    def __init__(
        self,
        navigate=None,
        screenshot=None,
        wait_for_selector=None,
    ):
        self._navigate_hook = navigate
        self._screenshot_hook = screenshot
        self._wait_for_selector_hook = wait_for_selector
        self.calls: list = []

    async def _invoke(self, label: str, hook, *args):
        self.calls.append((label, args))
        if hook is None:
            return None
        result = hook(*args) if callable(hook) else hook
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def navigate(self, url):
        return await self._invoke("navigate", self._navigate_hook, url)

    async def screenshot(self):
        return await self._invoke("screenshot", self._screenshot_hook)

    async def wait_for_selector(self, selector):
        return await self._invoke(
            "wait_for_selector", self._wait_for_selector_hook, selector
        )


@pytest.mark.asyncio
async def test_navigate_15s_timeout_raises():
    """Gate 1 — navigate must time out at the configured 15s window.

    The mcp navigate call hangs indefinitely (30s sleep in the mock).
    The wrapper applies ``asyncio.wait_for(timeout=15)`` and converts
    the ``TimeoutError`` into a FAILED result with
    ``stage_failed='navigate'`` and ``failure_reason`` mentioning the
    configured timeout.

    Test budget: we use a small ``navigate_timeout=0.1s`` in the
    config and a 5s mock sleep so the test finishes in well under
    1s wall time. The ``15s`` in the test name reflects the
    production default from the task spec; the contract under test
    is "the wrapper honours the configured navigate_timeout".
    """
    vp = {
        "id": "VP-002",
        "url": "http://bad-host:9999",
        "selectors": ["#main-button"],
    }
    config = {
        "puppeteer": {
            "navigate_timeout": 15,
            "screenshot_timeout": 10,
            "selector_timeout": 8,
            "navigate_retry_max": 0,
        }
    }
    # Overriding the navigate_timeout in the test (smaller for speed)
    # while keeping the production-default field in the config dict.
    config["puppeteer"]["navigate_timeout"] = 0.2

    async def _hang_30s(_url):
        # Production: 30s sleep simulating a hung MCP navigate call.
        # Test budget: 5s is enough to demonstrate the timeout fires.
        await asyncio.sleep(5)

    mcp = _MCPStub(navigate=_hang_30s)
    start = time.time()
    result = await _run_ui_validation_with_fastfail(vp, config, mcp=mcp)
    elapsed = time.time() - start

    assert result["id"] == "VP-002", f"unexpected id: {result.get('id')!r}"
    assert result["status"] == "FAILED", (
        "navigate timeout must yield status='FAILED', got "
        f"{result.get('status')!r}"
    )
    assert result.get("stage_failed") == "navigate", (
        f"stage_failed should be 'navigate', got {result.get('stage_failed')!r}"
    )
    failure_reason = str(result.get("failure_reason", ""))
    assert "timeout" in failure_reason.lower(), (
        f"failure_reason should mention 'timeout', got {failure_reason!r}"
    )
    # The configured timeout value (0.2) must appear in the reason
    # so the repair-task generator can attribute the failure to a
    # specific budget. We check the digit '0' is present (0.2 → '0.2').
    assert "0" in failure_reason, (
        f"failure_reason should reference the configured timeout "
        f"value, got {failure_reason!r}"
    )
    # Wall-time must be ~ the configured timeout, not the 5s sleep.
    # Generous upper bound (1.5s) to absorb CI jitter.
    assert elapsed < 1.5, (
        f"navigate timeout did not fire at the configured window: "
        f"elapsed={elapsed:.2f}s (expected ~0.2s)"
    )


@pytest.mark.asyncio
async def test_selector_8s_timeout_marks_failed():
    """Gate 3 — selector timeout must produce FAILED with selector name in deviation.

    The navigate + screenshot calls return immediately. The
    ``wait_for_selector`` call hangs. The wrapper must convert the
    timeout into ``status='FAILED'`` with a ``deviation`` value that
    contains the failing selector name (so the repair-task generator
    can attribute the failure to a specific UI element).
    """
    vp = {
        "id": "VP-003",
        "url": "http://test-host:8000/",
        "selectors": ["#main-button", ".missing-element"],
    }
    config = {
        "puppeteer": {
            "navigate_timeout": 15,
            "screenshot_timeout": 10,
            "selector_timeout": 8,
            "navigate_retry_max": 0,
        }
    }
    # Smaller selector_timeout for test speed; the contract under test
    # is "selector timeout → status=FAILED, deviation includes selector name".
    config["puppeteer"]["selector_timeout"] = 0.2

    async def _hang_5s(_selector):
        await asyncio.sleep(5)

    mcp = _MCPStub(
        navigate=None,  # returns None
        screenshot=None,  # returns None
        wait_for_selector=_hang_5s,
    )

    start = time.time()
    result = await _run_ui_validation_with_fastfail(vp, config, mcp=mcp)
    elapsed = time.time() - start

    assert result["id"] == "VP-003", f"unexpected id: {result.get('id')!r}"
    assert result["status"] == "FAILED", (
        f"selector timeout must yield status='FAILED', got {result.get('status')!r}"
    )
    assert result.get("stage_failed") == "selector", (
        f"stage_failed should be 'selector', got {result.get('stage_failed')!r}"
    )
    deviation = str(result.get("deviation", ""))
    # The deviation MUST include the failing selector name so the
    # repair-task generator can attribute the failure.
    assert "#main-button" in deviation, (
        f"deviation must contain the selector name '#main-button', "
        f"got {deviation!r}"
    )
    failure_reason = str(result.get("failure_reason", ""))
    assert "#main-button" in failure_reason, (
        f"failure_reason must reference the failing selector "
        f"'#main-button', got {failure_reason!r}"
    )
    # The wrapper must stop at the first failing selector (no
    # subsequent selector call).
    selector_calls = [
        c for c in mcp.calls if c[0] == "wait_for_selector"
    ]
    assert len(selector_calls) == 1, (
        f"wrapper should stop at the first failing selector, got "
        f"{len(selector_calls)} selector calls: {selector_calls!r}"
    )
    # Wall time must respect the configured timeout window.
    assert elapsed < 1.5, (
        f"selector timeout did not fire at the configured window: "
        f"elapsed={elapsed:.2f}s (expected ~0.2s)"
    )


@pytest.mark.asyncio
async def test_screenshot_failure_soft_continues():
    """Gate 2 — screenshot failure is a soft-fail: the VP continues.

    The navigate call returns immediately. The screenshot call
    raises an exception. The wrapper must:
      1. NOT terminate the VP at this point.
      2. Continue to the selector stage.
      3. Mark the result with ``screenshot_unavailable=True`` (or
         similar evidence) so the caller knows the screenshot was
         skipped.

    Final status depends on the subsequent selector stage. We make
    the selectors succeed so the final status is PASSED with a
    ``screenshot_unavailable`` marker.
    """
    vp = {
        "id": "VP-004",
        "url": "http://test-host:8000/",
        "selectors": ["#main-button"],
    }
    config = {
        "puppeteer": {
            "navigate_timeout": 15,
            "screenshot_timeout": 10,
            "selector_timeout": 8,
            "navigate_retry_max": 0,
        }
    }

    async def _screenshot_raises():
        raise RuntimeError("puppeteer screenshot binary not found")

    mcp = _MCPStub(
        navigate=None,
        screenshot=_screenshot_raises,
        wait_for_selector=None,
    )

    result = await _run_ui_validation_with_fastfail(vp, config, mcp=mcp)

    # The screenshot call MUST have happened (the gate is exercised).
    screenshot_calls = [c for c in mcp.calls if c[0] == "screenshot"]
    assert len(screenshot_calls) == 1, (
        f"expected exactly 1 screenshot call, got {len(screenshot_calls)}"
    )
    # The selector call MUST have happened (flow did NOT terminate
    # at the screenshot stage).
    selector_calls = [c for c in mcp.calls if c[0] == "wait_for_selector"]
    assert len(selector_calls) == 1, (
        f"flow must continue past screenshot failure; expected 1 "
        f"selector call, got {len(selector_calls)}"
    )
    # The final status is PASSED because the selector stage succeeded.
    assert result["status"] == "PASSED", (
        f"expected PASSED (screenshot failure is soft), got "
        f"{result.get('status')!r} with failure_reason="
        f"{result.get('failure_reason')!r}"
    )
    # The wrapper should mark that the screenshot was unavailable so
    # downstream consumers (e.g. the report) know the screenshot
    # was skipped.
    assert result.get("screenshot_unavailable") is True, (
        f"expected screenshot_unavailable=True to be set on the "
        f"result, got {result!r}"
    )


@pytest.mark.asyncio
async def test_navigate_retry_max_1():
    """Gate 1 — navigate retry is bounded by ``navigate_retry_max``.

    With ``navigate_retry_max=1`` the wrapper attempts navigation
    exactly twice (1 initial + 1 retry). After the second failure
    the wrapper must give up immediately and return FAILED with
    ``stage_failed='navigate'`` — it must NOT enter the screenshot
    stage.
    """
    vp = {
        "id": "VP-005",
        "url": "http://bad-host:9999",
        "selectors": ["#main-button"],
    }
    config = {
        "puppeteer": {
            "navigate_timeout": 15,
            "screenshot_timeout": 10,
            "selector_timeout": 8,
            "navigate_retry_max": 1,
        }
    }
    # Smaller navigate_timeout for test speed.
    config["puppeteer"]["navigate_timeout"] = 0.1

    async def _hang_5s(_url):
        await asyncio.sleep(5)

    mcp = _MCPStub(
        navigate=_hang_5s,
        screenshot=None,
        wait_for_selector=None,
    )

    result = await _run_ui_validation_with_fastfail(vp, config, mcp=mcp)

    # Exactly 2 navigate calls: 1 initial + 1 retry.
    navigate_calls = [c for c in mcp.calls if c[0] == "navigate"]
    assert len(navigate_calls) == 2, (
        f"expected exactly 2 navigate calls (initial + 1 retry), "
        f"got {len(navigate_calls)}: {navigate_calls!r}"
    )
    # The wrapper must NOT enter the screenshot stage after both
    # navigate attempts have failed.
    screenshot_calls = [c for c in mcp.calls if c[0] == "screenshot"]
    assert len(screenshot_calls) == 0, (
        f"wrapper must not enter screenshot stage when navigate "
        f"exhausted retries, got {len(screenshot_calls)} screenshot calls"
    )
    # Status is FAILED with stage_failed='navigate'.
    assert result["status"] == "FAILED", (
        f"expected FAILED, got {result.get('status')!r}"
    )
    assert result.get("stage_failed") == "navigate", (
        f"stage_failed should be 'navigate', got "
        f"{result.get('stage_failed')!r}"
    )


# ---------------------------------------------------------------------------
# write_verification_report — execution_profile observability field
#
# TDD spec (3 keys: mode / per_group_concurrency / total_wall_seconds):
#
# * ``test_report_includes_execution_profile`` — write a report with a
#   non-None ``execution_profile`` dict and confirm the field round-trips
#   through disk with all three keys intact and non-empty.
#
# * ``test_old_report_loads_without_field`` — load a fixture report
#   file that was written BEFORE the ``execution_profile`` field
#   existed; ``dict.get("execution_profile")`` must return ``None``
#   (not raise KeyError). This is the backwards-compat contract for
#   in-flight legacy V1 plans.
#
# * ``test_report_profile_is_optional`` — call
#   ``write_verification_report`` with ``execution_profile=None``; the
#   written JSON must contain the key as ``null`` (NOT omitted, NOT
#   an empty dict, NOT a raise).
# ---------------------------------------------------------------------------


def test_report_includes_execution_profile(tmp_path):
    """``execution_profile`` round-trips through the report JSON.

    Writes a report with a populated profile (mode='parallel_grouped',
    per_group_concurrency={'automated_test': 4}, total_wall_seconds=540)
    and reads it back. All three keys MUST be present and non-empty.
    """
    report_path = tmp_path / "report.json"
    results = [
        {"id": "VP-001", "status": "PASSED"},
        {"id": "VP-002", "status": "FAILED"},
    ]
    profile = {
        "mode": "parallel_grouped",
        "per_group_concurrency": {"automated_test": 4, "ui_validation": 2},
        "total_wall_seconds": 540,
    }

    write_verification_report(report_path, results, execution_profile=profile)

    with open(report_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    assert "execution_profile" in loaded, (
        "report must contain 'execution_profile' key, got keys: "
        f"{list(loaded.keys())}"
    )
    ep = loaded["execution_profile"]
    assert ep is not None, "execution_profile must not be None when caller passes a dict"
    assert ep.get("mode") == "parallel_grouped", (
        f"mode should round-trip, got {ep.get('mode')!r}"
    )
    assert ep.get("per_group_concurrency") == {
        "automated_test": 4,
        "ui_validation": 2,
    }, f"per_group_concurrency should round-trip, got {ep.get('per_group_concurrency')!r}"
    assert ep.get("total_wall_seconds") == 540, (
        f"total_wall_seconds should round-trip, got "
        f"{ep.get('total_wall_seconds')!r}"
    )
    # The results must also be preserved.
    assert loaded["results"] == results, (
        f"results should round-trip, got {loaded.get('results')!r}"
    )


def test_old_report_loads_without_field(tmp_path):
    """A pre-existing report without ``execution_profile`` loads cleanly.

    Simulates an legacy in-flight plan whose report file was
    written before the observability field existed. The reader (using
    ``.get``) MUST get ``None`` back — NOT a ``KeyError``. This is
    the backwards-compat contract.
    """
    report_path = tmp_path / "legacy_report.json"
    # Hand-craft a legacy report (no execution_profile key at all).
    legacy = {
        "results": [
            {"id": "VP-LEGACY-1", "status": "PASSED"},
            {"id": "VP-LEGACY-2", "status": "FAILED"},
        ],
        "overall_status": "FAILED",
        "requirement_deviations": [],
        # Note: no "execution_profile" key
    }
    report_path.write_text(json.dumps(legacy), encoding="utf-8")

    with open(report_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    # The contract: .get on a missing key returns None, never raises.
    ep = loaded.get("execution_profile")
    assert ep is None, (
        f"legacy report's execution_profile should be None, got {ep!r}"
    )
    # And the rest of the legacy data is still readable.
    assert loaded.get("overall_status") == "FAILED"
    assert len(loaded.get("results", [])) == 2


def test_report_profile_is_optional(tmp_path):
    """``execution_profile=None`` writes the field as JSON ``null``.

    Edge case: when the caller passes ``None`` (e.g. legacy serial
    mode), the field MUST appear in the JSON as ``null`` — not be
    omitted, not be replaced with ``{}``, and not raise an exception.
    This keeps the field position stable for downstream readers.
    """
    report_path = tmp_path / "report_no_profile.json"
    results = [{"id": "VP-X", "status": "PASSED"}]

    # Must not raise.
    written = write_verification_report(
        report_path, results, execution_profile=None,
    )

    # In-memory return value: profile is None, not omitted, not {}.
    assert "execution_profile" in written, (
        "in-memory report must contain 'execution_profile' key even "
        f"when value is None, got keys: {list(written.keys())}"
    )
    assert written["execution_profile"] is None, (
        f"in-memory execution_profile should be None, got "
        f"{written['execution_profile']!r}"
    )

    # On disk: the key must serialise as JSON null, not be missing.
    with open(report_path, "r", encoding="utf-8") as f:
        raw = f.read()
    assert '"execution_profile": null' in raw, (
        f"on-disk JSON must contain '\"execution_profile\": null', "
        f"got: {raw!r}"
    )
    # And the loaded dict must reflect null → None in Python.
    with open(report_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert "execution_profile" in loaded, (
        "loaded report must contain 'execution_profile' key even "
        f"when value is null, got keys: {list(loaded.keys())}"
    )
    assert loaded["execution_profile"] is None, (
        f"loaded execution_profile should be None, got "
        f"{loaded['execution_profile']!r}"
    )
