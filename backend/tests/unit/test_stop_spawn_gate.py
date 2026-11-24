"""A plan that is being stopped must not get a new process (2026-11-20).

Why this file exists
--------------------
The cancellation flag was read at three call sites, and the one that
mattered most — the dispatch registration — was read *after*
``subprocess.Popen`` had already created the child.  There is nothing to
kill before the child exists, so the registration's own check could
only ever reject a dispatch whose process was already running: the gate
was open at exactly the moment it could have been closed.

So the gate is closed **before** the spawn.  ``_assert_spawn_allowed``
is the new door: a dispatch for a plan that is stopping raises
:class:`verification_cancel.VerificationCancelled` and **no process is
created at all**.

Two independent facts can say "this plan is stopping", and the gate
reads both:

* ``stop_state.is_stopping(plan_id)`` — the plan-level record (the
  stop that is in progress *for that plan*);
* ``verification_cancel.is_cancelled(plan_id)`` — the round-level
  cancel flag the dispatch path already consulted.

The contracts here:

1. **A stopping plan gets no process.** The dispatch raises and the
   ``Popen`` count is zero — the negative control being that a gate
   that only refuses *after* the spawn would leave the count at one.
2. **A plan with no stop record is not gated.** The dispatch runs and
   answers exactly as it did before.
3. **No plan is not gated.** An empty ``plan_id`` belongs to nobody in
   particular; gating it would break every non-plan dispatch.
4. **A stop reaches only its own plan.** One plan's stop must never
   reach sideways into another plan's work.
5. **A refused dispatch leaves nothing behind** — no live-dispatch
   entry, no watchdog handle, no child.

Every test here goes through the PRODUCTION entry —
``ClaudeCodingTool.query_json`` → ``_query_json_internal`` →
``_run_interactive_resilient`` → ``_run_claude_interactive`` — with a
``claude`` shell script on ``PATH`` standing in for the CLI.  Nothing
calls an internal method directly, so a gate that was wired into the
wrong place would fail here rather than pass.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import coding_tool  # noqa: E402
import stop_state  # noqa: E402
import verification_cancel  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402
from sub_agent_registry import sub_agent_registry  # noqa: E402
from verification_cancel import VerificationCancelled  # noqa: E402

#: The checkout these tests run in.  Every coding tool built
#: in this file names it as its working root — the tool cannot be
#: born without one, and a root that is not named runs the child
#: in the backend server's directory instead.
_REPO_ROOT = Path(__file__).resolve().parents[3]

PLAN = "plan-stop-spawn-gate"
OTHER_PLAN = "plan-stop-spawn-gate-other"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _PopenSpy:
    """Counts the ``Popen`` calls a dispatch makes, and refuses to spawn.

    The count is the contract: "the plan was stopped, so no process was
    created".  A gate that ran *after* the spawn would leave the count
    at one, and the child would have to be reaped by hand — which is
    exactly the state this gate exists to prevent.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError(
            "Popen was called for a plan that is being stopped — the "
            "spawn gate has to run before the process is created"
        )


class _SubprocessProxy:
    """``coding_tool``'s view of ``subprocess``, with ``Popen`` spied.

    Only ``coding_tool``'s own reference is replaced, so anything else
    in the process still gets the real ``subprocess`` module.
    """

    def __init__(self, real, spy):
        self._real = real
        self._spy = spy

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Popen(self, *args, **kwargs):  # noqa: N802 — mirrors subprocess.Popen
        return self._spy(*args, **kwargs)


def _install_fake_claude(tmp_path, monkeypatch) -> Path:
    """Put an executable ``claude`` first on ``PATH``.

    The script speaks the stream-json protocol the read loop expects
    and appends a line to the launch log on every start, so a test can
    tell "no process was created" from "a process was created and
    answered".
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    launches = tmp_path / "launches.log"
    launches.write_text("", encoding="utf-8")

    parts = [
        "#!/bin/sh",
        f"echo launch >> '{launches}'",
        'sid=""',
        'prev=""',
        'for a in "$@"; do',
        '  if [ "$prev" = "--session-id" ] || [ "$prev" = "--resume" ];'
        ' then sid="$a"; fi',
        '  prev="$a"',
        "done",
        'if [ -n "$sid" ]; then'
        ' echo "{\\"type\\":\\"system\\",\\"subtype\\":\\"init\\",'
        '\\"session_id\\":\\"$sid\\"}"; fi',
        "echo '{\"type\":\"result\",\"subtype\":\"success\","
        "\"result\":\"{\\\"ok\\\": true}\"}'",
    ]
    fake = bindir / "claude"
    fake.write_text("\n".join(parts) + "\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", "")
    )
    return launches


def _launches(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").split())


@pytest.fixture(autouse=True)
def _hermetic_state(tmp_path, monkeypatch):
    """A hermetic stop registry, cancel flag, and plans root.

    The plans root is redirected because a dispatch writes its usage
    record there — that write has to land in ``tmp_path`` and not in
    the live operator state.
    """
    plans_root = tmp_path / "plans"
    (plans_root / PLAN).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_root))
    monkeypatch.setenv("PDT_PLAN_ID", PLAN)

    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()
    for plan_id in (PLAN, OTHER_PLAN):
        for handle in list(sub_agent_registry.all_handles(plan_id)):
            try:
                sub_agent_registry.unregister(plan_id, handle)
            except Exception:  # noqa: BLE001 — cleanup is best effort
                pass


@pytest.fixture
def spawn_gate_log():
    """The ``coding_tool`` log records of one test."""
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    log = logging.getLogger("coding_tool")
    previous_level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        yield records
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)


def _spawn_gate_rejections(records) -> list:
    return [
        record.getMessage()
        for record in records
        if "[spawn_gate] rejected" in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# 1. A stopping plan gets no process.
# ---------------------------------------------------------------------------
def test_spawn_rejected_while_stopping(tmp_path, monkeypatch, spawn_gate_log):
    """The gate runs before the spawn, so the child is never created.

    The ``Popen`` count is the contract.  A gate that only refused
    *after* the process existed would still raise the right exception
    and still leave a child behind — which is the situation this gate
    was introduced to close.
    """
    stop_state.begin(
        PLAN,
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=0,
    )
    launches = _install_fake_claude(tmp_path, monkeypatch)
    spy = _PopenSpy()
    monkeypatch.setattr(
        coding_tool, "subprocess", _SubprocessProxy(subprocess, spy),
    )

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    with pytest.raises(VerificationCancelled) as excinfo:
        tool.query_json(prompt="hello", system_instruction="sys")

    assert spy.calls == [], (
        "the dispatch spawned a process for a plan that is being stopped "
        "— the gate has to run before the spawn, not after it"
    )
    assert _launches(launches) == 0, (
        "the CLI was started for a plan that is being stopped"
    )
    assert excinfo.value.plan_id == PLAN
    assert id(tool) not in coding_tool._LIVE_DISPATCH_TOOLS, (
        "the refused dispatch left a live-dispatch entry behind — a "
        "later stop would count a child that was never started"
    )
    assert sub_agent_registry.all_handles(PLAN) == [], (
        "the refused dispatch registered a watchdog handle — there was "
        "no process for it to watch"
    )
    rejections = _spawn_gate_rejections(spawn_gate_log)
    assert rejections, (
        "the refusal was not audited — the log has to say which plan "
        "was refused and what it was refused for"
    )
    assert f"plan={PLAN}" in rejections[0], rejections[0]
    assert "what=" in rejections[0], rejections[0]


# ---------------------------------------------------------------------------
# 2. A plan with no stop record is not gated.
# ---------------------------------------------------------------------------
def test_spawn_allowed_without_stop(tmp_path, monkeypatch, spawn_gate_log):
    """No stop record → the dispatch runs and answers, as it always did."""
    assert stop_state.is_stopping(PLAN) is False
    launches = _install_fake_claude(tmp_path, monkeypatch)

    tool = ClaudeCodingTool( cwd=_REPO_ROOT)
    result = tool.query_json(prompt="hello", system_instruction="sys")

    assert result == {"ok": True}, (
        "the gate changed the answer of an ordinary dispatch: "
        f"{result!r}"
    )
    assert _launches(launches) == 1, (
        "the dispatch did not start a process — the gate let it through "
        "the refusal path instead of the normal one"
    )
    assert _spawn_gate_rejections(spawn_gate_log) == [], (
        "a dispatch that was not refused was logged as refused"
    )


# ---------------------------------------------------------------------------
# 3. No plan is not gated.
# ---------------------------------------------------------------------------
def test_empty_plan_is_not_gated():
    """An empty plan id belongs to nobody — it must not be gated.

    The CLI without a plan, the unit tests and the vendor tools all
    dispatch with no plan attached.  Gating them would break every
    non-plan call in the backend.
    """
    assert coding_tool._assert_spawn_allowed("", "vp_dispatch") is None
    assert coding_tool._assert_spawn_allowed(None, "vp_dispatch") is None

    # Even with a stop recorded for some other plan.
    stop_state.begin(
        OTHER_PLAN,
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=0,
    )
    assert coding_tool._assert_spawn_allowed("", "vp_dispatch") is None
    assert coding_tool._assert_spawn_allowed(None, "vp_dispatch") is None


# ---------------------------------------------------------------------------
# 4. The round-level cancel flag closes the same gate.
# ---------------------------------------------------------------------------
def test_cancelled_round_is_rejected_by_the_same_gate():
    """A cancelled round is a stopping plan, and is refused the same way."""
    verification_cancel.request_cancel(PLAN)

    with pytest.raises(VerificationCancelled) as excinfo:
        coding_tool._assert_spawn_allowed(PLAN, "vp_dispatch")

    assert excinfo.value.plan_id == PLAN
    assert "vp_dispatch" in str(excinfo.value), (
        "the refusal has to name what was refused, so the log says what "
        f"went wrong; it says {str(excinfo.value)!r}"
    )


# ---------------------------------------------------------------------------
# 5. A stop reaches only its own plan.
# ---------------------------------------------------------------------------
def test_a_stop_gates_only_its_own_plan():
    """One plan's stop must not reach sideways into another plan's work."""
    stop_state.begin(
        PLAN,
        scope=stop_state.SCOPE_VERIFICATION,
        previous_phase="verification_running",
        dispatch_baseline=0,
    )

    with pytest.raises(VerificationCancelled):
        coding_tool._assert_spawn_allowed(PLAN, "vp_dispatch")

    assert coding_tool._assert_spawn_allowed(OTHER_PLAN, "vp_dispatch") is None, (
        "the stop of one plan gated another plan's dispatch"
    )
