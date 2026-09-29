"""
Tests for the ``Attempt X of N`` bound rendered by
``RetryManager.get_retry_prompt_modifier``.

History
-------
2026-09-11: the prompt said ``of 2`` while the executor's loop ran 5,
so the fix hard-coded ``RetryManager.MAX_RETRIES = 5`` and asserted it
equalled ``AgentConfig.max_retries``.

2026-09-16: that fix was wrong in the same way it was trying to
correct. ``AgentConfig.max_retries`` is the *dataclass default* in
``config.py`` — not a value the runtime ever uses. ``AutonomousAgent``
builds its config from ``ConfigRegistry.get('coding')``
(``config_registry.py``, ``max_retries=2``), so the loop ran 2 attempts
while the prompt still said ``of 5``. Asserting equality against the
dataclass default passed the whole time because both sides of the
comparison were the same unused constant.

The structural fix: ``_execute_task_with_retry`` passes its own
``max_retries`` into the renderer, so the text follows the loop by
construction. These tests pin that:

  1. the rendered bound comes from the caller-supplied ``max_retries``;
  2. omitting it falls back to ``MAX_RETRIES`` (so non-executor callers
     keep working);
  3. the executor's real bound is the registry's, and that is the value
     the loop hands to the renderer;
  4. the call site in ``agent.py`` actually passes the bound — the bug
     was a wiring bug, so the guard has to be at the wiring.
"""

import os
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(BACKEND_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from retry_manager import RetryManager  # noqa: E402


def _executor_bound() -> int:
    """The bound ``AutonomousAgent`` actually runs with.

    ``AutonomousAgent.__init__`` does ``config or
    ConfigRegistry.get('coding')``, so the registry — not the
    ``AgentConfig`` dataclass default — is the operative source.
    """
    from config_registry import ConfigRegistry

    return int(ConfigRegistry.get("coding").max_retries)


def test_rendered_bound_follows_caller_supplied_max_retries():
    """The ``of N`` text must equal the bound the caller passes in."""
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    text = rm.get_retry_prompt_modifier("t1", 2)
    assert "Attempt 2 of 2" in text, text


def test_rendered_bound_tracks_a_different_value():
    """Same task, different bound → different text (no hard-coded 5)."""
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    assert "of 7" in rm.get_retry_prompt_modifier("t1", 7)
    assert "of 2" in rm.get_retry_prompt_modifier("t1", 2)


def test_bound_omitted_falls_back_to_class_default():
    """Non-executor callers keep the old behaviour."""
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    text = rm.get_retry_prompt_modifier("t1")
    assert f"Attempt 2 of {RetryManager.MAX_RETRIES}" in text, text


@pytest.mark.parametrize("bad", [None, 0, -1, "", "abc"])
def test_invalid_bound_falls_back(bad):
    """A falsy/garbage bound must not render ``of 0`` / ``of None``."""
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    text = rm.get_retry_prompt_modifier("t1", bad)
    assert f"of {RetryManager.MAX_RETRIES}" in text, text


def test_executor_renders_the_registry_bound():
    """The executor's real bound (registry) is what gets rendered."""
    bound = _executor_bound()
    assert bound >= 1
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    assert f"Attempt 2 of {bound}" in rm.get_retry_prompt_modifier("t1", bound)


def test_dataclass_default_is_not_assumed_to_be_the_bound():
    """Documents the 2026-09-11 mistake so it is not repeated.

    Comparing the rendered text against ``AgentConfig.max_retries``
    proved nothing, because the runtime takes its config from the
    registry. The renderer must follow whatever bound it is handed,
    even when that disagrees with the dataclass default.
    """
    from backend.config import AgentConfig

    operative = _executor_bound()
    rm = RetryManager()
    rm.record_attempt("t1", "boom", success=False)
    text = rm.get_retry_prompt_modifier("t1", operative)
    assert f"of {operative}" in text
    if int(AgentConfig.max_retries) != operative:
        assert f"of {AgentConfig.max_retries}" not in text, (
            "rendered text followed 'AgentConfig.max_retries' (the "
            "unused dataclass default) instead of the bound it was "
            "handed"
        )


def test_call_site_passes_the_bound():
    """Guard the wiring, not just the renderer.

    The original bug was ``agent.py`` calling the renderer without a
    bound, so a renderer-only test would have stayed green through it.
    """
    src = (BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    # Only real code — several docstrings/comments quote the old
    # signature as prose, and matching those would be a false positive.
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    calls = re.findall(
        r"get_retry_prompt_modifier\((.*?)\)", code, re.DOTALL
    )
    assert calls, "agent.py no longer calls get_retry_prompt_modifier"
    for args in calls:
        assert "max_retries" in args, (
            "get_retry_prompt_modifier called without a max_retries "
            f"bound: get_retry_prompt_modifier({args.strip()})"
        )


def test_prompt_modifier_empty_for_unknown_task():
    """Unknown task_id → empty modifier (no retry context yet)."""
    rm = RetryManager()
    assert rm.get_retry_prompt_modifier("never_recorded") == ""
    assert rm.get_retry_prompt_modifier("never_recorded", 2) == ""
