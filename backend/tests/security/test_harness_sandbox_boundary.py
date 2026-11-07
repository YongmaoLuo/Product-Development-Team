"""SEC-01..07 — the boundary a harness run may not cross.

What is pinned here
-------------------

Four failure modes have one shape in common: the boundary is
*configured* but *silently ineffective*. A sandbox profile that is
present but never applied, a workspace write that leaves the
workspace, a symlink that points at the checkout, a ``..`` that
climbs out — each of them can leave an operator looking at a green
run and a file that changed anyway.

The sandbox is the sharpest version of the shape, because the
operator's configuration says it is on. ``claude_sandbox`` refuses to
degrade: a profile that is configured and cannot be applied is a
configuration error, not a reason to run the agent anyway. These
cases pin that refusal from both sides — the run that *is* confined,
and the run that says out loud that it is not.

Every case here is proved in both directions: the thing that must be
refused **is** refused, and the thing that must be allowed **is**
allowed. A suite that only proves refusals cannot tell a boundary
from a blanket denial — a runner that refused every write would pass
every "denied" assertion and be useless.

The cases
---------

===  ==========================  =========================================
SEC  Case                        Boundary
===  ==========================  =========================================
01   an out-of-scope write       the kernel refuses a write outside the
                                 workspace the profile names
02   an in-scope write           the same profile allows the write
                                 inside it — the denial above is a
                                 boundary, not a blanket refusal
03   a policy that cannot run    nothing is started at all
04   no policy at all            the run says it was unsandboxed
05   a symlink at the checkout   the adapter refuses the target and the
                                 checkout's bytes do not change
06   a ``..`` that climbs out    refused before a byte is written
07   ``<dataDir>/mcp.json``      refused by an explicit check
===  ==========================  =========================================

The observation point
--------------------

Two of the cases are about the kernel, so they need a real sandbox:
``sandbox-exec`` is a macOS facility, and a profile that is refused
by the kernel on one platform is simply not applicable on another.
Those two cases skip where there is no sandbox to run, and the five
that are about *code* run everywhere — a refusal that only exists on
one platform is not a refusal.

The child is a real process, not a mock: it attempts one write and
records what the kernel said about it. The verdict is always the
**file's own state**, never the child's exit code. A model that took
a different path, or a child that reported an error instead of
leaving one, must not be able to turn a case green or red.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from claude_sandbox import SandboxUnavailableError
from harness.contract import HarnessConfigError, HarnessInvocation
from harness.mcode import McodeHarness, _assert_scope
from harness.runner import Runner

#: The checkout this suite runs in — the "main checkout" a workspace
#: write must never be able to reach. Derived from this file rather
#: than written out, so the suite says the same thing on every
#: machine and the path never becomes a machine-specific constant.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]

#: The file inside the checkout that SEC-05 aims a symlink at. It is
#: tracked, it is small, and it is a file whose contents nobody would
#: want a stray write to change.
_CHECKOUT_VICTIM = _CHECKOUT_ROOT / "README.md"

#: The MCP configuration the write-containment cases use. The
#: contents do not matter — the cases are about *where* the adapter
#: is willing to put them.
_CONFIG = {"mcpServers": {"x": {"url": "http://example.invalid"}}}

#: The sandbox wrapper is a macOS facility. Where it is missing, a
#: configured profile is a configuration error rather than something
#: that can be applied, and the cases that need a real kernel cannot
#: be run at all.
requires_macos_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="the sandbox wrapper is macOS-only",
)


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------

#: The program a sandboxed run executes.
#:
#: It attempts exactly one write and records what happened, so the
#: observable is the file and the kernel's answer to it. The report is
#: written where the profile allows writes, and the target is wherever
#: the case under test wants it — that difference is the whole point
#: of SEC-01 and SEC-02.
#:
#: The child writes its report even when the write was refused: a
#: child that could not report would leave the case with no evidence
#: at all, and "no evidence" must not be able to pass for "refused".
_PROBE = r'''
import json
import os
import sys

target = sys.argv[1]
report = sys.argv[2]

outcome = {
    "target": target,
    "wrote": False,
    "error": "",
    "sandbox_marker": os.environ.get("PDT_SANDBOX_ACTIVE", ""),
}

try:
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("written by the probe\n")
    outcome["wrote"] = True
except OSError as exc:
    outcome["error"] = "{}: {}".format(type(exc).__name__, exc)

outcome["type"] = (
    "tool_call_completed" if outcome["wrote"] else "tool_call_denied"
)

with open(report, "w", encoding="utf-8") as handle:
    json.dump(outcome, handle, sort_keys=True)

print(json.dumps(outcome, sort_keys=True))
'''


def _invocation(
    workspace: Path, target: Path, report: Path
) -> HarnessInvocation:
    """An invocation that runs the probe against ``target``.

    Args:
        workspace: The directory the child runs in.
        target: The file the child tries to write.
        report: Where the child records what happened.

    Returns:
        The invocation, pointing at the probe.
    """
    return HarnessInvocation(
        argv=(sys.executable, "-c", _PROBE, str(target), str(report)),
        # Bytecode caching would make the child attempt a write of its
        # own, which is a second thing for a profile to allow or deny.
        env_delta={"PYTHONDONTWRITEBYTECODE": "1"},
        cwd=str(workspace),
    )


def _report(path: Path) -> Dict[str, Any]:
    """The probe's own account of the write it attempted.

    Args:
        path: Where the probe was told to write its report.

    Returns:
        The report, as a mapping.

    Raises:
        AssertionError: The probe wrote no report. A child that never
            started and a child that was stopped early are both
            "no evidence", and neither may pass for a refusal.
    """
    assert path.exists(), (
        f"the probe wrote no report at {path} — the child either never "
        f"started, or was stopped before it could say what happened"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _denied_tool_calls(result: Any) -> List[Dict[str, Any]]:
    """The tool calls in a run's stream that were refused.

    Args:
        result: The result of a run.

    Returns:
        The payloads of the events that report a refused write. The
        adapter keeps an event it does not know rather than dropping
        it, so the refusal rides along in the event's ``raw``.
    """
    denied = []
    for event in result.events:
        raw = event.raw
        if not isinstance(raw, dict):
            continue
        # The adapter keeps an event it does not know rather than
        # dropping it. Where it keeps it is the adapter's business:
        # one puts the payload straight into ``raw``, the other nests
        # it one level down.
        for candidate in (raw, raw.get("raw")):
            if (
                isinstance(candidate, dict)
                and candidate.get("type") == "tool_call_denied"
            ):
                denied.append(candidate)
    return denied


def _sha256(path: Path) -> str:
    """The digest of a file's bytes.

    Args:
        path: The file to digest.

    Returns:
        The hex digest.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile(tmp_path: Path, workspace: Path) -> Path:
    """A Seatbelt profile that allows writes inside ``workspace`` only.

    The workspace is resolved before it goes into the profile: macOS
    matches a sandbox rule against the *real* path, and the temporary
    directory a test runs in is reached through a symlink. A profile
    written with the unresolved path would allow nothing at all — which
    is exactly what the in-scope case is there to catch.

    Args:
        tmp_path: The directory the profile is written into.
        workspace: The only directory writes are allowed in.

    Returns:
        The path of the profile.
    """
    profile = tmp_path / "profile.sb"
    profile.write_text(
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{workspace.resolve()}"))\n',
        encoding="utf-8",
    )
    return profile


def _no_profile(monkeypatch, tmp_path: Path) -> None:
    """Make sure no sandbox is configured for the run.

    Args:
        monkeypatch: The fixture that restores the environment.
        tmp_path: The directory used as the configuration directory.
    """
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir(exist_ok=True)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(empty_config))


def _workspace(tmp_path: Path) -> Tuple[Path, Path]:
    """A workspace, and a directory outside it.

    Args:
        tmp_path: The directory the two are created in.

    Returns:
        The workspace and the directory beside it.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return workspace, outside


# ---------------------------------------------------------------------------
# SEC-01 — a write outside the scope is refused
# ---------------------------------------------------------------------------


@requires_macos_sandbox
def test_out_of_scope_write_denied(tmp_path, monkeypatch):
    """SEC-01 — the kernel refuses a write outside the workspace.

    The verdict is the file's own state, not the child's exit code. A
    child that took a different path, or that reported an error
    instead of leaving one, must not be able to turn this case green
    or red — the boundary is about what happened to the file.
    """
    workspace, outside = _workspace(tmp_path)

    victim = outside / "victim.txt"
    victim.write_text("untouched\n", encoding="utf-8")
    before = _sha256(victim)

    profile = _profile(tmp_path, workspace)
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    report = workspace / "probe.json"
    invocation = _invocation(workspace, victim, report)

    result = Runner().run(McodeHarness(), invocation)

    assert result.sandbox_state == "applied"

    # The file is what the boundary is about: it did not change.
    assert _sha256(victim) == before
    assert victim.read_text(encoding="utf-8") == "untouched\n"

    # And the kernel said why — the refusal was the sandbox's, not
    # the file system's.
    outcome = _report(report)
    assert outcome["wrote"] is False
    assert "not permitted" in outcome["error"].lower(), (
        f"the write failed for a reason that is not the sandbox's: "
        f"{outcome['error']!r}"
    )

    # The run's own stream says the same thing, so a reader of the
    # result sees the refusal without going to the file to look.
    assert _denied_tool_calls(result), (
        "the run's stream did not carry the denied tool call"
    )


# ---------------------------------------------------------------------------
# SEC-02 — a write inside the scope is allowed
# ---------------------------------------------------------------------------


@requires_macos_sandbox
def test_in_scope_write_succeeds(tmp_path, monkeypatch):
    """SEC-02 — the same profile allows the write that is inside it.

    Without this case SEC-01 proves nothing: a profile that denied
    every write would pass it. The boundary has to have an inside, and
    the inside has to be the workspace and nothing else.
    """
    workspace, _ = _workspace(tmp_path)

    profile = _profile(tmp_path, workspace)
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    target = workspace / "inside.txt"
    report = workspace / "probe.json"
    invocation = _invocation(workspace, target, report)

    result = Runner().run(McodeHarness(), invocation)

    assert result.sandbox_state == "applied"

    # The write landed, with the bytes the probe wrote.
    assert target.exists(), "the in-scope write did not happen"
    assert target.read_text(encoding="utf-8") == "written by the probe\n"

    outcome = _report(report)
    assert outcome["wrote"] is True
    assert outcome["error"] == ""
    assert not _denied_tool_calls(result), (
        "the run's stream carried a denial for a write that was allowed"
    )


# ---------------------------------------------------------------------------
# SEC-03 — a policy that cannot be applied starts nothing
# ---------------------------------------------------------------------------


def test_broken_policy_starts_nothing(tmp_path, monkeypatch):
    """SEC-03 — a policy that cannot be applied starts no child.

    Two ways to be broken, and both have to fail the same way: a
    profile the kernel refuses to parse, and a wrapper that cannot be
    executed at all. Neither may leave a half-started child behind —
    the refusal happens before the spawn, so there is nothing to clean
    up and nothing to report.
    """
    workspace, _ = _workspace(tmp_path)

    # (a) The profile exists, and it is not a profile. The kernel
    #     refuses it, and the refusal is a configuration error rather
    #     than a reason to run the agent anyway.
    broken = tmp_path / "broken.sb"
    broken.write_text("this is not a seatbelt profile\n", encoding="utf-8")
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(broken))

    report = workspace / "probe.json"
    invocation = _invocation(workspace, workspace / "target.txt", report)

    with pytest.raises(SandboxUnavailableError):
        Runner().run(McodeHarness(), invocation)

    assert not report.exists(), "a child started under a broken profile"
    assert not (workspace / "target.txt").exists()

    # (b) The profile is fine, and the wrapper cannot be run: the
    #     ``sandbox-exec`` on the PATH is not executable. A profile
    #     that cannot be applied is an error here as well — the same
    #     error, from the same place.
    monkeypatch.setenv(
        "PDT_SANDBOX_PROFILE", str(_profile(tmp_path, workspace))
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    wrapper = fake_bin / "sandbox-exec"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(wrapper, 0o000)
    # The PATH is replaced rather than extended: a wrapper that is not
    # executable is skipped over by the lookup, so the real one would
    # still be found behind it.
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(SandboxUnavailableError):
        Runner().run(McodeHarness(), invocation)

    assert not report.exists(), "a child started with an unusable wrapper"
    assert not (workspace / "target.txt").exists()


# ---------------------------------------------------------------------------
# SEC-04 — no policy means the run says so
# ---------------------------------------------------------------------------


def test_no_policy_reports_unsandboxed(tmp_path, monkeypatch):
    """SEC-04 — an unconfigured deployment says it was not confined.

    The token is the point. A reader of a result has to be able to
    tell a confined run from an unconfined one without reading the
    machine's configuration — and the default install has to keep
    working exactly as it did before the sandbox existed.
    """
    _no_profile(monkeypatch, tmp_path)

    workspace, _ = _workspace(tmp_path)
    target = workspace / "written.txt"
    report = workspace / "probe.json"

    result = Runner().run(McodeHarness(), _invocation(workspace, target, report))

    assert result.sandbox_state == "unsandboxed"

    # The run happened, and nothing claimed it was confined.
    outcome = _report(report)
    assert outcome["sandbox_marker"] == ""
    assert target.exists(), "the run did not happen"


# ---------------------------------------------------------------------------
# SEC-05 and SEC-06 — the escapes the adapter refuses
# ---------------------------------------------------------------------------


def test_symlink_and_dotdot_rejected(tmp_path):
    """SEC-05 and SEC-06 — two escapes, one refusal.

    A symlink that points at the checkout and a ``..`` that climbs out
    of the workspace are the same escape wearing two different names,
    and both are refused before a single byte is written. The
    checkout's own bytes are the evidence: a refusal that still
    changed them would be a refusal in name only.
    """
    harness = McodeHarness()

    # SEC-05: the workspace's ``.mcp.json`` is a symlink at a file in
    # the checkout. The adapter resolves both sides before it writes,
    # so the link cannot smuggle the write out of the workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert _CHECKOUT_VICTIM.exists(), (
        f"{_CHECKOUT_VICTIM} is not there — the checkout this suite runs "
        f"in does not have the file the symlink aims at"
    )
    original = _CHECKOUT_VICTIM.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    link = workspace / ".mcp.json"
    link.symlink_to(_CHECKOUT_VICTIM)

    written = False
    try:
        with pytest.raises(HarnessConfigError):
            harness.mcp_prepare(workspace, _CONFIG)
        written = _CHECKOUT_VICTIM.read_bytes() != original
    finally:
        # A guard that failed would have overwritten the file it was
        # there to protect. Put the bytes back, so that a failing case
        # cannot damage the checkout it is about.
        if _CHECKOUT_VICTIM.read_bytes() != original:
            _CHECKOUT_VICTIM.write_bytes(original)

    assert written is False, "the checkout's file was written"
    assert hashlib.sha256(_CHECKOUT_VICTIM.read_bytes()).hexdigest() == before
    assert link.is_symlink(), "the link was replaced instead of refused"

    # SEC-06: a ``..`` that climbs out of the workspace. The adapter's
    # own target is always ``<workspace>/.mcp.json``, so the traversal
    # is put to the guard every write goes through — the one that
    # decides whether a target may be written at all.
    escape = tmp_path / "outside" / "config.json"
    with pytest.raises(HarnessConfigError):
        _assert_scope(workspace / ".." / "outside" / "config.json", workspace)

    assert not escape.exists(), "the escaping target was written"

    # And the guard is a boundary, not a blanket refusal: a target
    # inside a workspace is accepted, and the adapter writes it. A
    # clean workspace, because the one above still holds the link
    # that was refused — and a refused link stays refused.
    clean = tmp_path / "clean-workspace"
    clean.mkdir()
    inside = clean / ".mcp.json"
    with harness.mcp_prepare(clean, _CONFIG):
        assert inside.exists()
        assert json.loads(inside.read_text(encoding="utf-8")) == _CONFIG

    assert not inside.exists(), "the workspace was not put back"


# ---------------------------------------------------------------------------
# SEC-07 — the account's own registry is never written to
# ---------------------------------------------------------------------------


def test_data_dir_mcp_json_rejected(tmp_path, monkeypatch):
    """SEC-07 — ``<dataDir>/mcp.json`` is refused by an explicit check.

    mcode keeps the user's own MCP servers in ``<dataDir>/mcp.json``.
    A workspace that *is* that directory is a configuration mistake,
    and writing there would replace the user's servers with the
    adapter's. The check is explicit — the scope check cannot catch
    it, because the target is inside the workspace it was given.
    """
    data_dir = tmp_path / "minimax"
    data_dir.mkdir()
    registry = data_dir / "mcp.json"
    registry.write_text('{"mcpServers": {"mine": {}}}\n', encoding="utf-8")
    before = _sha256(registry)
    monkeypatch.setenv("MINIMAX_DATA_DIR", str(data_dir))

    harness = McodeHarness()
    with pytest.raises(HarnessConfigError):
        harness.mcp_prepare(data_dir, _CONFIG)

    assert _sha256(registry) == before, "the account's registry changed"
    assert not (data_dir / ".mcp.json").exists()
