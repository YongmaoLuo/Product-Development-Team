"""The sub-agent's child process must be told which mode the plan runs in.

A plan may declare that it runs in ``test`` mode: the pipeline is
exercised, but the run must not touch anything real.  The mode has to
reach the processes the plan spawns -- otherwise the cost gate believes
the plan is in ``test`` mode while the sub-agent believes it is in
``production``, and the run does the opposite of what it was asked to do.

The channel is the ``--settings`` payload's ``env``: the CLI folds its
settings scopes into ``process.env`` late enough to win over anything
the child inherited.  So the mode rides the same payload as the other
``PDT_*`` stamps.

The properties pinned here:

* a plan that says ``test`` puts ``PDT_PLAN_MODE=test`` into the payload;
* a tool with **no** mode writes **no** key -- an ordinary coding task
  must not carry a mode it was never given;
* a mode nobody recognises falls back to ``production``, because that is
  the mode that must be safe to assume (see :mod:`plan_mode`);
* the scoped verification tool reads the mode off the plan's own state,
  so the sub-agent and the cost gate cannot disagree about it.

Every test that touches the plan state points ``PDT_STATE_DB_PATH`` at
its own temporary database first, so nothing here can read or write the
operator's real state store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def test_stamp_injects_mode():
    """A plan that says ``test`` says so to its children."""
    from coding_tool import _stamp_plan_mode

    settings = {}
    _stamp_plan_mode(settings, "test")

    assert settings["env"]["PDT_PLAN_MODE"] == "test"


def test_stamp_skips_when_none():
    """No mode means no key -- and no ``env`` invented either.

    An ordinary coding task carries no plan mode, and the payload it
    hands the CLI must not grow one just because the stamp exists.
    """
    from coding_tool import _stamp_plan_mode

    settings = {}
    _stamp_plan_mode(settings, None)

    assert "PDT_PLAN_MODE" not in settings.get("env", {})

    # An empty mode is "no mode" too -- it is what a plan that says
    # nothing looks like from the outside.
    _stamp_plan_mode(settings, "")
    assert "PDT_PLAN_MODE" not in settings.get("env", {})


def test_stamp_invalid_falls_back_to_production():
    """A mode nobody recognises resolves the way every other reader does.

    ``resolve_mode`` is the one parser in the codebase; the stamp must
    not invent a second answer, and it must not pass an unknown value
    through to the child either.
    """
    from coding_tool import _stamp_plan_mode

    settings = {}
    _stamp_plan_mode(settings, "prod")

    assert settings["env"]["PDT_PLAN_MODE"] == "production"


def test_scoped_plan_mode_reads_plan_state(tmp_path, monkeypatch):
    """The scoped tool's mode comes off the plan's own state row.

    Same reader the cost gate uses, so the two cannot disagree: the
    sub-agent is told ``test`` exactly when the plan says ``test``.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState
    from verification_subagent import _scoped_plan_mode

    plan_dir = tmp_path / "plan-scoped-mode"
    plan_dir.mkdir(parents=True, exist_ok=True)
    ps = PlanState(plan_dir)
    ps.ensure_row()
    ps.set_mode("test")

    assert _scoped_plan_mode(plan_dir) == "test"


def test_the_dispatched_settings_payload_carries_the_mode(
    tmp_path, monkeypatch
):
    """The wiring: the payload the CLI is handed carries the mode.

    The cases above pin the seams; this one pins the road between them.
    The ``--settings`` payload is what the child process reads (the CLI
    folds its settings scopes into ``process.env`` late enough to win),
    so a mode that did not reach the file did not reach the child.

    No subprocess is spawned: ``subprocess.Popen`` is patched, so only
    the ``cmd`` handed to it and the settings file it names are
    inspected.  Everything private goes under ``tmp_path``, so the test
    cannot touch the operator's real ``~/.pdt-scratch``.
    """
    monkeypatch.setenv("PDT_SECRET_TEMP_ROOT", str(tmp_path / "scratch"))
    # Read the settings file as *written*, not as redacted -- redaction
    # has its own suite, and the mode is not a secret.
    monkeypatch.setattr("coding_tool.redact_all", lambda *a, **k: 0)

    from coding_tool import ClaudeCodingTool
    from subagent_config import SubagentConfig

    template = SubagentConfig(
        provider_name="vendor-a-pro",
        base_url="https://vendor-a.example.invalid/anthropic",
        api_key="sk-template-key",
        auth_token="sk-template-token",
    ).write_tmp_settings()

    tool = ClaudeCodingTool(
        api_key="sk-test-key",
        base_url="https://vendor-a.example.invalid/anthropic",
        auth_token="sk-test-token",
        # A sentinel chain keeps the dispatch from resolving a real
        # CC Switch row (and leaking live credentials into the file).
        provider_priority=["__no_such_provider__"],
        settings=str(template),
        plan_mode="test",
    )
    assert tool.plan_mode == "test"

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "result", "result": "ok", "is_error": False}) + "\n",
    ])
    mock_proc.stderr.read.return_value = ""
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0

    with patch("coding_tool.subprocess.Popen") as mock_popen:
        mock_popen.return_value = mock_proc
        tool.query("test prompt")

    cmd = mock_popen.call_args[0][0]
    assert "--settings" in cmd, f"cmd missing '--settings': {cmd!r}"
    settings_path = Path(cmd[cmd.index("--settings") + 1])
    payload = json.loads(settings_path.read_text(encoding="utf-8"))

    assert payload["env"]["PDT_PLAN_MODE"] == "test"
