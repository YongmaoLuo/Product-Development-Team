"""E2E-MCODE — the real MiniMax Code backend, end to end.

Why this scenario exists
========================

Every layer below this one runs against a fake CLI or a recorded
stream. A fake cannot answer the two questions the replacement of the
Claude Code backend rests on:

  * does mcode really **read** the ``.mcp.json`` the adapter injects
    into the workspace (it has no ``--mcp-config``; the workspace file
    is the only channel), and
  * does the real macOS seatbelt the runner applies really **confine**
    the child, or is the confinement only ever claimed.

So this scenario runs the real thing: a real ``mcode`` process, spawned
by the real unified dispatch path (``coding_tool._run_claude_interactive``
— the one place task 18 put every spawn), under the real seatbelt, with
a real MCP server configured through the workspace's ``.mcp.json``, in a
real server process that answers a real guarded HTTP request.

The task is fixed so the run is comparable and cheap: *create
``hello.txt`` in the run's working directory and ``cat`` it back*.

The nine assertions
====================

They are the ones the test design (decision point 6) fixes for this
scenario, and each one is answered by evidence produced by the run
itself — never by a recorded fixture:

  1. the execution directory is outside ``backend/`` (the guard that
     stops a plan from executing inside the service's own source tree);
  2. the run exited 0, produced a ``result`` event, and reported no
     error;
  3. ``hello.txt`` exists and its content hash is the expected one;
  4. the usage block carries all four canonical keys and
     ``input_tokens > 0``;
  5. the session id the CLI minted has the ``mvs_`` shape;
  6. the pinned session id is a uuid5 derived from the plan and the
     task, and the audit sidecar agrees with it;
  7. ``.mcp.json`` is gone (or restored) afterwards and git never saw
     it;
  8. the run was confined: the runner reports ``sandbox_state ==
     "applied"``, the process table shows the very process the runner
     spawned still running while the run was in flight, and a write
     outside the workspace was denied with a permission error;
  9. nothing was left running when the run ended.

Why the assertions are split over three tests
=============================================

``test_mcode_full_chain`` carries all nine, because the scenario is
one run and the nine facts are one run's facts. The two focused tests
re-state two of them on their own, because those two are the ones a
future change is most likely to break silently: an execution directory
that drifts under ``backend/``, and a child that outlives its run.

Cost and blast radius
=====================

The scenario costs one real model call and spawns a real server, so it
is off unless ``PDT_E2E_HARNESS=1`` is set: the local and CI
deterministic gates only *collect* it. It is marked ``real_model`` (it
really calls a model) and ``e2e`` (it drives a real process), so the
cloud lanes skip it twice over.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import uuid

import pytest

#: The variable that turns the scenario on. Without it the module is
#: collected and skipped — the deterministic gates never pay for a real
#: model call.
E2E_ENV_VAR = "PDT_E2E_HARNESS"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.environ.get(E2E_ENV_VAR) != "1",
        reason=(
            "the real mcode chain needs PDT_E2E_HARNESS=1: it spawns the "
            "real mcode CLI under the real macOS seatbelt and costs a model "
            "call, so the deterministic gates only collect it"
        ),
    ),
]

#: The shape of a session id the MiniMax Code CLI mints. A pinned id is
#: a key, not a session id — the CLI cannot create a conversation under
#: a chosen one — so the id that comes back must look like this.
MCODE_SESSION_RE = re.compile(r"^mvs_[0-9a-f]{32}$")

#: The canonical usage block: four keys, no more. The fourth is ``None``
#: when the CLI never reported cache creation — that is a missing
#: measurement, not a zero.
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
)

#: What the fixed task must leave behind.
HELLO_TEXT = "hello e2e"
HELLO_WITH_NEWLINE = f"{HELLO_TEXT}\n".encode("utf-8")
HELLO_WITHOUT_NEWLINE = HELLO_TEXT.encode("utf-8")
HELLO_HASHES = frozenset(
    {
        hashlib.sha256(HELLO_WITH_NEWLINE).hexdigest(),
        hashlib.sha256(HELLO_WITHOUT_NEWLINE).hexdigest(),
    }
)


def test_mcode_full_chain(mcode_chain):
    """All nine assertions, answered by one real run.

    Args:
        mcode_chain: The facts one real mcode dispatch produced.
    """
    facts = mcode_chain

    # -- 1: the execution directory is outside the service's own tree ----
    backend_dir = facts.server.backend_dir.resolve()
    workspace = facts.workspace.resolve()
    assert backend_dir not in workspace.parents, (
        f"the run executed inside the backend source tree: {workspace}"
    )
    assert not str(workspace).startswith(f"{backend_dir}{os.sep}"), (
        f"the run executed inside the backend source tree: {workspace}"
    )

    # -- 2: the run itself succeeded -------------------------------------
    assert facts.result.exit_code == 0, (
        f"the real mcode run exited {facts.result.exit_code}; "
        f"stderr/events: {facts.stderr_excerpt()}"
    )
    assert facts.result.is_error is False, (
        "the real mcode run reported an error"
    )
    assert any(event.type == "result" for event in facts.result.events), (
        "the real mcode run produced no result event"
    )

    # -- 3: the fixed task's artefact ------------------------------------
    assert facts.hello_path.is_file(), (
        f"the fixed task left no hello.txt in {facts.workspace}"
    )
    hello_bytes = facts.hello_path.read_bytes()
    assert hashlib.sha256(hello_bytes).hexdigest() in HELLO_HASHES, (
        f"hello.txt does not carry the expected content; got "
        f"{hello_bytes!r}"
    )
    assert hello_bytes.decode("utf-8").strip() == HELLO_TEXT
    assert HELLO_TEXT in facts.text, (
        "the run did not cat hello.txt back"
    )

    # -- 4: usage ---------------------------------------------------------
    usage = facts.result.usage
    assert usage is not None, "the real mcode run reported no usage"
    assert set(usage) == USAGE_KEYS, (
        f"the usage block does not carry the four canonical keys: "
        f"{sorted(usage)}"
    )
    assert usage["input_tokens"] > 0, (
        f"the run reported no input tokens: {usage}"
    )

    # -- 5: the session the CLI minted ------------------------------------
    assert facts.result.session_id, "the real mcode run reported no session"
    assert MCODE_SESSION_RE.match(facts.result.session_id), (
        f"the session id is not a MiniMax Code one: "
        f"{facts.result.session_id!r}"
    )

    # -- 6: the pinned id is a uuid5, and the audit agrees ----------------
    assert uuid.UUID(facts.pinned_session_id).version == 5, (
        f"the pinned session id is not a uuid5: {facts.pinned_session_id!r}"
    )
    assert facts.pinned_session_id == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"pdt:{facts.plan_id}:{facts.step_key}")
    ), (
        f"the pinned session id is not the one derived from the plan and "
        f"the task: {facts.pinned_session_id!r}"
    )
    assert facts.sidecar.get(facts.step_key) == facts.pinned_session_id, (
        f"the audit sidecar does not agree with the pinned id: "
        f"{facts.sidecar!r}"
    )
    assert facts.resolved_session_id == facts.pinned_session_id, (
        "the dispatch did not run under the pinned session id"
    )

    # -- 7: the injected MCP configuration is gone again ------------------
    assert facts.mcp_json_seen, (
        "the workspace never held a .mcp.json while the run was in "
        "flight, so 'it is gone now' would be true of a file that was "
        "never there"
    )
    assert not (facts.workspace / ".mcp.json").exists(), (
        "the injected .mcp.json was left behind in the workspace"
    )
    assert ".mcp.json" not in facts.git_status, (
        f"git saw the injected .mcp.json: {facts.git_status!r}"
    )
    assert "hello.txt" in facts.git_status, (
        f"git saw nothing at all, so the .mcp.json check is vacuous: "
        f"{facts.git_status!r}"
    )

    # -- 8: the run really was confined ----------------------------------
    assert facts.result.sandbox_state == "applied", (
        f"the run was not confined: sandbox_state="
        f"{facts.result.sandbox_state!r}"
    )
    assert facts.runner_pid in facts.sampled_pids, (
        f"the process table never showed the process the runner spawned "
        f"(pid {facts.runner_pid}); sampled {facts.sampled_pids}; "
        f"rows {facts.sampled_rows!r}"
    )
    assert facts.marker_path.is_file(), (
        "the MCP server the workspace configuration named was never "
        "started, so mcode did not read .mcp.json"
    )
    assert not facts.escape_path.exists(), (
        "a write outside the workspace succeeded, so the seatbelt did "
        "not confine the run"
    )
    assert facts.escape_errno in (errno.EACCES, errno.EPERM), (
        "the write outside the workspace failed for some reason other "
        f"than a permission refusal: errno={facts.escape_errno!r}"
    )

    # -- 9: nothing was left running -------------------------------------
    assert facts.orphans == [], (
        f"the run left processes behind: {facts.orphans}"
    )


def test_workspace_outside_backend(mcode_chain):
    """The execution directory is not inside ``backend/``.

    The service refuses a plan whose project directory sits under its
    own source tree; the run must not have found a way around that by
    executing somewhere else either.

    Args:
        mcode_chain: The facts one real mcode dispatch produced.
    """
    facts = mcode_chain
    backend_dir = facts.server.backend_dir.resolve()
    workspace = facts.workspace.resolve()

    assert backend_dir not in workspace.parents, (
        f"the run executed inside the backend source tree: {workspace}"
    )
    assert facts.invocation_cwd == facts.workspace, (
        f"the dispatch ran in {facts.invocation_cwd!r}, not in the "
        f"workspace {facts.workspace}"
    )


def test_no_orphan_after_run(mcode_chain):
    """The run left no process behind.

    The scan is keyed on the scenario's own temporary tree and on this
    process's own children: a process of somebody else's that happens
    to be running is none of this test's business, and a process of
    this test's that is still there is exactly what this test is about.

    Args:
        mcode_chain: The facts one real mcode dispatch produced.
    """
    facts = mcode_chain

    assert facts.orphans == [], (
        f"the run left processes behind: {facts.orphans}"
    )
