"""One backend per process, and every dispatch reads the same one.

What this pins
--------------
Choosing a backend used to be a decision made in five places inside
``coding_tool.py``: the plain text query, the JSON query, the sub-agent
query (the one that asks for a restricted tool set), the retry that
follows an unusable answer, and the recovery that re-opens a session
each built its own command line, and each decided for itself which
backend it was talking to. Five decisions, five places to forget one.

This module pins the three properties that make one decision enough:

1. **One object.** Every dispatch kind hands the spawn the same
   harness, so "which backend is this process driving" has exactly one
   answer — and the answer is an object, not a name that has to be
   compared somewhere.
2. **One moment.** The backend is resolved when the process starts.
   Changing the environment afterwards changes nothing: a run cannot
   change backend halfway through because the environment moved under
   it.
3. **One refusal.** A backend this process cannot honour fails at
   import — before the first dispatch is paid for, and before any
   dispatch is entered at all.

Why two of the three need a fresh interpreter
---------------------------------------------
Two of these properties are about *import time*, and an import happens
once per process. ``coding_tool`` is already in ``sys.modules`` by the
time a test runs, so a test that changed the environment and re-imported
would be testing the module cache rather than the start-up contract.
Those cases therefore run in a fresh interpreter, where the import
really does happen under the environment the test set up.

The dispatch kinds are driven with the spawn replaced (the pattern the
other ``coding_tool`` unit tests use): the harness is what the dispatch
hands the spawn, so the spawn is where it is observed — and nothing
real is spawned, nothing real is written, and no provider is contacted.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

#: The backend package root — ``<repo>/backend``.
_BACKEND_DIR = Path(__file__).resolve().parents[3]

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402

pytestmark = pytest.mark.unit


#: The code that asks the process which backend it is driving.
_ASK = "import coding_tool; print(coding_tool.current_harness().name)"

#: The code that changes the environment *after* the process started.
_CHANGE_AFTER_START = (
    "import os\n"
    "import coding_tool\n"
    "os.environ['PDT_HARNESS'] = 'mcode'\n"
    "print(coding_tool.current_harness().name)\n"
)

#: The code that imports the module under a value the process cannot
#: honour, and reports which exception stopped it.
#:
#: The exception is caught *by class*, so the child can only report a
#: refusal that really is a :class:`HarnessConfigError` — a program
#: that died for any other reason prints nothing and exits zero, and
#: the test reads that as "the import was not refused at all".
#: ``IMPORTED`` is printed only when the import got through, so its
#: absence is what proves the refusal happened *at* the import rather
#: than somewhere after it.
_REFUSED_AT_IMPORT = (
    "import sys\n"
    "from harness.contract import HarnessConfigError\n"
    "try:\n"
    "    import coding_tool\n"
    "except HarnessConfigError as exc:\n"
    "    print('REFUSED', type(exc).__name__, exc)\n"
    "    sys.exit(7)\n"
    "print('IMPORTED')\n"
)


def _run_python(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter that can import ``coding_tool``.

    The child inherits this process's environment with the backend
    variable removed, so the "nothing configured" default is observable
    and the shell of whoever runs the test cannot decide it.

    Args:
        code: The program the child runs.

    Returns:
        The completed process — its exit code, stdout and stderr.
    """
    env = dict(os.environ)
    env.pop("PDT_HARNESS", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def recorded_dispatches(monkeypatch):
    """A tool whose spawn is replaced by a recorder.

    The recorder captures the harness each dispatch handed the spawn, so
    a test can ask "which backend did this dispatch use?" without
    spawning anything.

    Yields:
        The list of harnesses the dispatches handed the spawn, in the
        order they were handed over.
    """
    # The rate-limit backoff's waits are replaced, not served: a
    # dispatch whose provider answers 429 waits that provider out, and
    # a unit test is about the decision rather than the patience of the
    # suite.
    monkeypatch.setattr(coding_tool, "_rate_limit_sleep", lambda seconds: None)
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {},
    )

    seen: list = []

    def _tool(responses):
        """A tool whose spawn answers with ``responses``, in order."""
        tool = coding_tool.ClaudeCodingTool()
        tool.logger = MagicMock()
        remaining = list(responses)

        def _spawn(prompt, *args, **kwargs):
            seen.append(kwargs.get("harness"))
            text = remaining.pop(0) if remaining else "{}"
            return text, "session-id", {}

        monkeypatch.setattr(tool, "_run_claude_interactive", _spawn)
        return tool

    yield seen, _tool


# ---------------------------------------------------------------------------
# 1. Every dispatch kind reads the same harness
# ---------------------------------------------------------------------------


def test_all_dispatches_share_harness(recorded_dispatches):
    """Five dispatch kinds, one harness object.

    The five kinds are the plain text query, the JSON query, the
    sub-agent query (the one that asks for a restricted tool set), the
    retry that follows an unusable answer, and the recovery that
    re-opens a session. They are five different questions, asked five
    different ways — and they all have to be answered by the same
    backend.
    """
    seen, make_tool = recorded_dispatches
    harness = coding_tool.current_harness()

    # 1. The plain text query.
    tool = make_tool(["done"])
    assert tool.query("do the task", timeout=10) == "done"

    # 2. The JSON query.
    tool = make_tool(['{"ok": true}'])
    assert tool.query_json("give me json") == {"ok": True}

    # 3. The sub-agent query — the one that asks for a restricted tool
    #    set, which is the restriction that must never be dropped
    #    silently.
    tool = make_tool(['{"ok": true}'])
    assert tool.query_json("restricted", allowed_tools=["Read"]) == {"ok": True}

    # 4. The retry — the first answer cannot be parsed, so the same
    #    question is asked again in the same conversation.
    tool = make_tool(["not json at all", '{"ok": true}'])
    assert tool.query_json("give me json") == {"ok": True}

    # 5. The recovery — the session that died is re-opened.
    tool = make_tool(["done"])
    text, _session_id = tool._resume_or_fresh_session(
        prev_session_id="session-id",
        resume_prompt="continue",
        fresh_prompt="do the task",
        system_instruction=None,
        excluded_providers=None,
        total_timeout=None,
        allowed_tools=None,
        scene=None,
    )
    assert text == "done"

    assert seen, "no dispatch reached the spawn"
    for one in seen:
        assert one is not None, (
            "a dispatch reached the spawn without a harness — it would "
            "have had to pick a backend of its own"
        )
        assert one is harness, (
            "a dispatch reached the spawn with a different harness than "
            "the process drives"
        )
    assert len({id(one) for one in seen}) == 1, (
        "the five dispatch kinds did not share one harness object"
    )


def test_the_selection_point_is_not_in_coding_tool():
    """The backend is chosen by the registry, not by this module.

    ``coding_tool`` reads the environment variable that names the
    backend nowhere, and it never compares a backend *name* — a
    comparison on a name is a second selection point wearing a
    disguise, and it is the thing the harness layer exists to remove.
    """
    source = Path(coding_tool.__file__).read_text(encoding="utf-8")

    # The variable that names the backend is read by the registry alone.
    assert not re.search(
        r"""os\.environ[^\n]*["']PDT_HARNESS["']""", source
    ), "coding_tool reads the backend variable itself"

    # No comparison against a backend name, in any spelling.
    forbidden = re.compile(
        r"""(?:==|!=)\s*["'](?:claude|mcode)["']"""
        r"""|["'](?:claude|mcode)["']\s*(?:==|!=)"""
        r"""|\bin\s*[\(\[\{][^)\]\}]*["'](?:claude|mcode)["']"""
        r"""|\bcase\s+["'](?:claude|mcode)["']"""
    )
    match = forbidden.search(source)
    assert match is None, (
        f"coding_tool compares a backend name at offset {match.start() if match else 0}; "
        "the backend is a capability, not a name"
    )


# ---------------------------------------------------------------------------
# 2. The answer is resolved once, at start-up
# ---------------------------------------------------------------------------


def test_env_change_after_start_is_ignored(monkeypatch, frozen_env):
    """Changing the environment after the start changes nothing.

    Two things are checked: in this process, the object the accessor
    answers with does not change; and in a process that starts with
    nothing configured and changes the environment afterwards, the
    backend is still the one that was resolved at start-up.
    """
    before = coding_tool.current_harness()

    monkeypatch.setenv("PDT_HARNESS", "mcode")

    after = coding_tool.current_harness()
    assert after is before
    assert id(after) == id(before)

    # The same, in a process where the import really does happen before
    # the environment changes.
    result = _run_python(_CHANGE_AFTER_START)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "claude", (
        "the backend changed after the process started"
    )


# ---------------------------------------------------------------------------
# 3. A backend this process cannot honour is refused at import
# ---------------------------------------------------------------------------


def test_invalid_env_fails_at_import(frozen_env):
    """A value the process cannot honour stops it before it dispatches.

    The refusal has to happen at import, not at the first dispatch: a
    process that reads a wrong value, shrugs and runs the default
    backend is a process whose operator believes something the process
    does not believe — and the belief only surfaces in the bill.
    """
    result = _run_python("import coding_tool")
    assert result.returncode == 0, result.stderr

    env = dict(os.environ)
    env["PDT_HARNESS"] = "MCODE"
    failed = subprocess.run(
        [sys.executable, "-c", _REFUSED_AT_IMPORT],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # The refusal is a ``HarnessConfigError`` — caught by class, not by
    # the accident of a word appearing somewhere in a traceback.
    assert failed.returncode == 7, failed.stdout + failed.stderr
    assert failed.stdout.startswith("REFUSED"), failed.stdout
    assert "HarnessConfigError" in failed.stdout, failed.stdout

    # It happened at the import: the statement after it never ran.
    assert "IMPORTED" not in failed.stdout, failed.stdout

    # And the refusal says what was wrong and what was allowed.
    assert "PDT_HARNESS" in failed.stdout, failed.stdout
    assert "claude" in failed.stdout, failed.stdout
    assert "mcode" in failed.stdout, failed.stdout
