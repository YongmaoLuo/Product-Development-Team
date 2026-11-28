"""Integration spec for the Runner — the one spawn path.

What is pinned here
-------------------

Every way this codebase drives a coding agent ends the same way: build
an argv, spawn a process, read a stream of events out of it, and decide
what the exit code meant. The Runner is the one place that happens, so
these four cases are about the *order* of that path and about what it
refuses to do:

1. **IT-RUN-01 — the child really ran inside the sandbox.** A runner
   that applies a profile but spawns the bare argv is worse than one
   that applies nothing: the operator's configuration says the run was
   confined, and nothing is. The proof is the kernel's own enforcement,
   not a marker — see the note on the ancestor chain below.
2. **IT-RUN-02 — no profile means no sandbox, and no failure.** The
   default install must keep working exactly as it did before the
   runner existed.
3. **IT-RUN-03 — a profile that cannot be applied starts nothing.** The
   error is raised *before* the spawn, so there is no half-started
   child, no output file, and nothing to clean up.
4. **IT-RUN-10 — two adapters, one path.** The claude adapter and the
   MiniMax Code adapter are different command lines, but they are
   spawned by the same code; a second spawn path would be a second set
   of bugs.

The observation point
--------------------

The child is a real process: ``tests/fixtures/bin/harness_probe_cli.py``
stands in for a coding CLI, records what it was handed, and writes the
record where this spec can read it. Nothing here is stubbed — the
thing under test is the spawn itself.

A note on the ancestor chain
----------------------------

The spec for IT-RUN-01 says "the probe's ancestor chain contains the
sandbox wrapper". On macOS that chain cannot contain it, and the reason
is worth writing down rather than papering over:

* ``sandbox-exec`` **replaces itself** with the command it runs, so
  the wrapper's command line is not left anywhere in the process tree.
  (``ps`` from outside a sandboxed run shows the child's own command
  line and no ``sandbox-exec`` at all.)
* ``ps`` is setuid root, and macOS refuses to exec a setuid binary
  from inside a sandbox, so a confined probe cannot list the process
  table either.

What does survive is the kernel's enforcement, and that is what this
spec asserts: the profile denies reading one specific file, the probe
tries to read it, and the kernel refuses. A run that was not confined
reads the file and says so. The wrapper's environment marker is
asserted as well, because it is what a descendant uses to know that it
is confined.
"""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_sandbox import SandboxUnavailableError
from harness.claude import ClaudeHarness
from harness.contract import HarnessInvocation
from harness.mcode import McodeHarness
from harness.runner import Runner

pytestmark = pytest.mark.integration

#: The fake CLI the runner is pointed at.
PROBE = Path(__file__).resolve().parents[2] / "fixtures" / "bin" / "harness_probe_cli.py"

#: The sandbox wrapper is a macOS facility; on any other platform a
#: configured profile is a configuration error rather than something
#: that can be applied.
requires_macos_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="the sandbox wrapper is macOS-only",
)


def _profile_for(tmp_path: Path, denied: Path) -> Path:
    """A profile that applies, and denies reading exactly one file.

    The denial is what makes a confined run distinguishable from an
    unconfined one: the kernel refuses the read in the first case and
    allows it in the second.
    """
    profile = tmp_path / "profile.sb"
    profile.write_text(
        "(version 1)\n"
        "(allow default)\n"
        f'(deny file-read* (subpath "{denied.resolve()}"))\n',
        encoding="utf-8",
    )
    return profile


def _invocation(tmp_path: Path, out: Path, **extra: str) -> HarnessInvocation:
    """An invocation that runs the probe and writes its report to ``out``."""
    env = {"PROBE_OUT": str(out), "PROBE_EXIT": "0"}
    env.update(extra)
    return HarnessInvocation(
        argv=(sys.executable, str(PROBE), "--flag"),
        env_delta=env,
        cwd=str(tmp_path),
    )


def _no_profile(monkeypatch, tmp_path: Path) -> None:
    """Make sure no sandbox is configured for the run."""
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir(exist_ok=True)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(empty_config))


# ---------------------------------------------------------------------------
# IT-RUN-01 — the child really ran inside the sandbox
# ---------------------------------------------------------------------------


@requires_macos_sandbox
def test_probe_is_wrapped(tmp_path, monkeypatch):
    """The spawned child is confined, not merely marked as confined."""
    denied = tmp_path / "denied.txt"
    denied.write_text("not for the sandboxed run\n", encoding="utf-8")
    profile = _profile_for(tmp_path, denied)
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    out = tmp_path / "probe.json"
    invocation = _invocation(
        tmp_path,
        out,
        PROBE_DENY_PATH=str(denied.resolve()),
    )

    result = Runner().run(ClaudeHarness(), invocation, stdin_text="hi")

    # The runner reports what it did ...
    assert result.sandbox_state == "applied"
    assert result.exit_code == 0

    # ... and the child agrees, in the only way that cannot be faked:
    # the kernel refused an operation the profile denies.
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["sandbox_enforced"] is True, (
        "the child was not confined — the profile's denial was not in force"
    )

    # The wrapper's marker reached the child, so a descendant of this
    # run knows it is confined.
    assert report["sandbox_marker"] == str(profile)

    # The child saw the argv the runner was given, plus the flag the
    # invocation added. (``sys.argv`` inside the child starts at the
    # script, so the interpreter is not part of it.)
    assert report["argv"] == [str(PROBE), "--flag"]

    # The prompt arrived on stdin, and the pipe was closed — the probe
    # reads to EOF, so a runner that forgot to close it would hang.
    assert report["stdin"] == "hi\n"


# ---------------------------------------------------------------------------
# IT-RUN-02 — no profile means no sandbox, and no failure
# ---------------------------------------------------------------------------


def test_unsandboxed_when_no_policy(tmp_path, monkeypatch):
    """An unconfigured deployment spawns exactly as it always did."""
    _no_profile(monkeypatch, tmp_path)

    out = tmp_path / "probe.json"
    invocation = _invocation(tmp_path, out)

    result = Runner().run(ClaudeHarness(), invocation, stdin_text="hi")

    assert result.sandbox_state == "unsandboxed"
    assert result.exit_code == 0
    assert result.harness == "claude"

    # The run happened, and nothing claimed it was confined.
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["sandbox_marker"] == ""
    assert report["sandbox_enforced"] is None


# ---------------------------------------------------------------------------
# IT-RUN-03 — a profile that cannot be applied starts nothing
# ---------------------------------------------------------------------------


def test_broken_policy_starts_nothing(tmp_path, monkeypatch):
    """A broken profile is a configuration error, and no child is spawned."""
    profile = tmp_path / "broken.sb"
    profile.write_text("this is not a seatbelt profile\n", encoding="utf-8")
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    out = tmp_path / "probe.json"
    invocation = _invocation(tmp_path, out)

    with pytest.raises(SandboxUnavailableError):
        Runner().run(ClaudeHarness(), invocation, stdin_text="hi")

    # The child never started: it wrote nothing, and there is no
    # half-run to clean up.
    assert not out.exists()


# ---------------------------------------------------------------------------
# The preparation context manager — it holds the whole run
# ---------------------------------------------------------------------------


def test_prepare_wraps_the_whole_run(tmp_path, monkeypatch):
    """The resource the invocation reserves is held for the whole run."""
    _no_profile(monkeypatch, tmp_path)

    marker = tmp_path / "held.marker"

    @contextmanager
    def _prepare():
        marker.write_text("held\n", encoding="utf-8")
        try:
            yield
        finally:
            marker.unlink(missing_ok=True)

    out = tmp_path / "probe.json"
    invocation = HarnessInvocation(
        argv=(sys.executable, str(PROBE), "--flag"),
        env_delta={"PROBE_OUT": str(out), "PROBE_EXIT": "0"},
        cwd=str(tmp_path),
        prepare=lambda: _prepare(),
    )

    result = Runner().run(ClaudeHarness(), invocation, stdin_text="hi")

    assert result.exit_code == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    # The child ran while the resource was held: the file the context
    # manager prepared was in its working directory.
    assert "held.marker" in report["cwd_entries"]
    # ... and the resource was released once the run was over.
    assert not marker.exists()


# ---------------------------------------------------------------------------
# IT-RUN-10 — two adapters, one path
# ---------------------------------------------------------------------------

def test_two_harnesses_share_path(tmp_path, monkeypatch):
    """The claude adapter and the mcode adapter are spawned the same way."""
    _no_profile(monkeypatch, tmp_path)

    results = []
    reports = []
    for harness, name in ((ClaudeHarness(), "claude"), (McodeHarness(), "mcode")):
        out = tmp_path / f"probe-{name}.json"
        invocation = _invocation(tmp_path, out)
        results.append(Runner().run(harness, invocation, stdin_text="hi"))
        reports.append(json.loads(out.read_text(encoding="utf-8")))

    # Same path, same shape of result — only the adapter's name differs.
    assert [result.harness for result in results] == ["claude", "mcode"]
    assert {result.sandbox_state for result in results} == {"unsandboxed"}
    assert {result.exit_code for result in results} == {0}
    assert all(result.outcome.kind == "ok" for result in results)

    # Both children were spawned the same way, and both were told the
    # same things.
    assert reports[0]["argv"] == reports[1]["argv"]
    assert reports[0]["cwd"] == reports[1]["cwd"]
    assert reports[0]["stdin"] == reports[1]["stdin"] == "hi\n"
    assert reports[0]["sandbox_marker"] == reports[1]["sandbox_marker"] == ""
