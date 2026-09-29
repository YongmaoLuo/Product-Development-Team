"""Contract tests: the per-VP timeout interface is DELETED (2026-09-13).

2026-09-13: the timeout contract is exactly two layers —
  1. 1-hour outer hard cap: flat ``HARD_WALL_CLOCK_CAP_SECONDS = 3600``
     in VerificationSubAgent (no per-VP scaling, no multiplier, no grace)
  2. 15-min inner idle: ``coding_tool.DEFAULT_TOTAL_TIMEOUT = 900``

A legacy plan's ``timeout_seconds`` field (e.g. VP-023's 120) must never
be read again. These static + behavioural tests pin the contract so a
future refactor cannot silently re-introduce the per-VP override.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SUBAGENT_PY = _BACKEND_DIR / "verification_subagent.py"
_AGENT_PY = _BACKEND_DIR / "verification_agent.py"


# ---------------------------------------------------------------------------
# 1. Static — no per-VP timeout reads remain
# ---------------------------------------------------------------------------


def test_no_vp_timeout_reads_remain_in_subagent():
    """verification_subagent.py must not reference the deleted resolver
    nor read a per-VP ``timeout_seconds`` field from any vp_node."""
    src = _SUBAGENT_PY.read_text(encoding="utf-8")
    assert "_resolve_vp_timeout_seconds" not in src, (
        "_resolve_vp_timeout_seconds was deleted 2026-09-13 — do not "
        "re-introduce the per-VP timeout interface."
    )
    assert 'vp_node.get("timeout_seconds"' not in src, (
        "verification_subagent.py must not read a per-VP "
        "timeout_seconds field anymore."
    )
    assert "OUTER_TIMEOUT_MULTIPLIER" not in src, (
        "OUTER_TIMEOUT_MULTIPLIER was deleted 2026-09-13 — the outer "
        "cap is flat, no scaling."
    )
    assert "QUERY_ABANDON_GRACE_SECONDS" not in src, (
        "QUERY_ABANDON_GRACE_SECONDS was deleted 2026-09-13 — the "
        "outer cap is flat 3600, no grace arithmetic."
    )


def test_no_resolve_with_vp_payload_in_agent():
    """verification_agent.py must call TimeoutPolicy.resolve() without
    a vp payload argument (method-level resolution only)."""
    import re

    src = _AGENT_PY.read_text(encoding="utf-8")
    # Any .resolve(<method>, <something>) call is a violation.
    m = re.search(r"timeout_policy\.resolve\(\s*[^)]+,", src)
    assert m is None, (
        f"verification_agent.py still passes a second argument to "
        f"timeout_policy.resolve(): {m.group(0) if m else ''!r}. The "
        f"vp-payload override was deleted 2026-09-13."
    )


# ---------------------------------------------------------------------------
# 2. Behavioural — flat 1-hour cap, no scaling knobs
# ---------------------------------------------------------------------------


def test_outer_cap_is_flat_3600():
    from verification_subagent import VerificationSubAgent

    assert VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS == 3600
    assert not hasattr(VerificationSubAgent, "OUTER_TIMEOUT_MULTIPLIER")
    assert not hasattr(VerificationSubAgent, "QUERY_ABANDON_GRACE_SECONDS")


def test_subagent_has_no_resolve_method():
    from verification_subagent import VerificationSubAgent

    assert not hasattr(VerificationSubAgent, "_resolve_vp_timeout_seconds")


def test_schema_conversion_pins_3600():
    """A legacy plan carrying timeout_seconds (even the round-4-stalling
    120) must be converted to the flat 3600 value."""
    from verification_agent import VerificationAgent

    plan = {
        "verification_points": [
            {
                "id": "VP-023",
                "verification_method": "automated_test",
                "title": "nightly CI",
                "test_command": "docker compose up -d && pytest tests/ -v",
                "timeout_seconds": 120,
            },
        ]
    }
    out = VerificationAgent._convert_plan_to_executor_schema(plan)
    assert out["vps"][0]["timeout_seconds"] == 3600


def test_resolve_ignores_legacy_payload_value():
    """TimeoutPolicy.resolve() is method-level only — a legacy payload
    dict carrying timeout_seconds cannot change the result."""
    from verification_config import TimeoutPolicy

    policy = TimeoutPolicy.defaults()
    assert policy.resolve("automated_test") == 3600
    assert policy.resolve("automated_test") == policy.global_default()


def test_coding_tool_defaults_unchanged():
    """The 15-min idle default and the 1h constant in coding_tool must
    remain untouched (they are the inner enforcement layer)."""
    from coding_tool import ClaudeCodingTool

    assert ClaudeCodingTool.DEFAULT_TOTAL_TIMEOUT == 900
    assert ClaudeCodingTool.HARD_WALL_CLOCK_TIMEOUT_SECONDS == 3600
