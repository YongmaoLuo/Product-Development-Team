"""A configured sub-agent sandbox must either apply or raise.

2026-10-06. Sub-agents were spawned as ``claude ... --permission-mode
bypassPermissions`` with no confinement, so an agent had unrestricted
filesystem access and no gate anywhere in the path.

The module that closes that gap is deliberately asymmetric, and this
file pins each arm of it:

* not configured → unsandboxed spawn, byte-identical to before;
* configured and applicable → the argv is wrapped;
* configured and not applicable → :class:`SandboxUnavailableError`.

The third arm is the one that matters. A sandbox that quietly turns
itself off is worse than no sandbox, because the operator's
configuration still claims it is on — which is exactly how a
machine-wide ``~/bin/claude`` wrapper once left ``/sandbox`` reporting
"sandboxed" while enforcing nothing, and how a plan then died at task 1
with eleven consecutive ``sandbox_apply: Operation not permitted``.

The error text is part of the contract: it has to name the cause and
the fix, because the operator hitting it is looking at a subprocess
refusal with no other context.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import claude_sandbox  # noqa: E402
from claude_sandbox import (  # noqa: E402
    SandboxUnavailableError,
    already_sandboxed,
    describe,
    wrap_command,
)

CMD = ["claude", "--verbose", "--permission-mode", "bypassPermissions"]

# A minimal but genuinely applicable Seatbelt profile: it confines writes
# to the system paths and leaves everything else alone, so applying it in
# a test actually exercises sandbox-exec rather than a mock.
_APPLICABLE = """(version 1)
(allow default)
(deny file-write*
  (subpath "/System"))
"""

# Unparseable, so the kernel refuses it — the stand-in for "configured
# but will not apply".
_BROKEN = "this is not a seatbelt profile\n"


@pytest.fixture(autouse=True)
def _no_ambient_profile(monkeypatch):
    """No test should inherit a profile from the developer's own setup."""
    monkeypatch.delenv("PDT_SANDBOX_PROFILE", raising=False)

#: ``_validate`` checks the platform before it checks anything the tests
#: below mock out, so a test that never reaches ``sandbox-exec`` still
#: needs the macOS gate. Without it the whole group fails on a Linux
#: runner with "sandbox-exec is a macOS facility" — which is the
#: production behaviour working correctly and the test being unable to
#: see past it. The one case that deliberately exercises the non-macOS
#: branch (``test_non_macos_raises_rather_than_ignoring_the_profile``)
#: is *not* gated, because it sets the platform itself.
_REQUIRES_MACOS = pytest.mark.skipif(
    sys.platform != "darwin", reason="sandbox-exec is macOS-only"
)


# ---------------------------------------------------------------------------
# Not configured — the default must be exactly what it always was
# ---------------------------------------------------------------------------


def test_unconfigured_spawn_is_untouched(monkeypatch):
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None,
    )
    decision = wrap_command(CMD)

    assert decision.command == CMD, (
        "an unconfigured deployment must spawn the identical argv — a "
        "sandbox feature that changes default behaviour is not opt-in"
    )
    assert decision.reason == "not_configured"
    assert decision.profile is None
    assert decision.sandboxed is False


def test_unconfigured_does_not_touch_env(monkeypatch):
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None,
    )
    env = {"PATH": "/usr/bin"}
    wrap_command(CMD, env)
    assert claude_sandbox.ACTIVE_ENV not in env


@_REQUIRES_MACOS
def test_caller_command_list_is_not_mutated(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _APPLICABLE)))
    original = list(CMD)
    wrap_command(CMD)
    assert CMD == original, "wrap_command must not mutate the caller's list"


# ---------------------------------------------------------------------------
# Configured and applicable
# ---------------------------------------------------------------------------


@_REQUIRES_MACOS
def test_configured_profile_wraps_the_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _APPLICABLE)))
    decision = wrap_command(CMD)

    assert decision.reason == "applied"
    assert decision.sandboxed is True
    assert decision.command[0].endswith("sandbox-exec")
    assert decision.command[1:3] == ["-f", decision.profile.as_posix()]
    assert "--" in decision.command, (
        "a -- separator keeps a leading-dash argument in the wrapped "
        "command from being read as one of sandbox-exec's own"
    )
    assert decision.command[-len(CMD):] == CMD


@_REQUIRES_MACOS
def test_wrapped_env_records_the_profile(monkeypatch, tmp_path):
    profile = _write(tmp_path, _APPLICABLE)
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))
    env = {"PATH": "/usr/bin"}
    decision = wrap_command(CMD, env)

    assert env[claude_sandbox.ACTIVE_ENV] == str(decision.profile), (
        "a descendant must be able to tell that it is confined"
    )


@_REQUIRES_MACOS
def test_profile_content_is_left_alone(monkeypatch, tmp_path):
    profile = _write(tmp_path, _APPLICABLE)
    before = profile.read_text(encoding="utf-8")
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))
    wrap_command(CMD)
    assert profile.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Configured and not applicable — fail closed, loudly
# ---------------------------------------------------------------------------


def test_missing_profile_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(tmp_path / "absent.sb"))
    with pytest.raises(SandboxUnavailableError) as excinfo:
        wrap_command(CMD)
    assert "does not exist" in str(excinfo.value)
    assert str(tmp_path / "absent.sb") in str(excinfo.value)


def test_unparseable_profile_raises_rather_than_spawning(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _BROKEN)))
    with pytest.raises(SandboxUnavailableError):
        wrap_command(CMD)


@_REQUIRES_MACOS
def test_error_names_the_cause_and_the_fix(monkeypatch, tmp_path):
    """The operator sees a subprocess refusal and nothing else."""
    monkeypatch.setattr(claude_sandbox, "already_sandboxed", lambda: True)
    monkeypatch.setattr(claude_sandbox, "_sandbox_exec", lambda: "/usr/bin/sandbox-exec")
    profile = _write(tmp_path, _APPLICABLE)
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(profile))

    def _refuse(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 71, "", "sandbox-exec: sandbox_apply: Operation not permitted",
        )

    monkeypatch.setattr(subprocess, "run", _refuse)
    with pytest.raises(SandboxUnavailableError) as excinfo:
        wrap_command(CMD)
    message = str(excinfo.value)
    assert "already inside an OS sandbox" in message
    assert "identical profile nests fine" in message
    assert "sandbox_apply" in message, (
        "the kernel's own words are the only hard evidence of the cause"
    )


def test_non_macos_raises_rather_than_ignoring_the_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _APPLICABLE)))
    monkeypatch.setattr(claude_sandbox.sys, "platform", "linux")
    with pytest.raises(SandboxUnavailableError) as excinfo:
        wrap_command(CMD)
    assert "macOS facility" in str(excinfo.value)


@_REQUIRES_MACOS
def test_missing_sandbox_exec_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _APPLICABLE)))
    monkeypatch.setattr(claude_sandbox, "_sandbox_exec", lambda: None)
    with pytest.raises(SandboxUnavailableError) as excinfo:
        wrap_command(CMD)
    assert "not on PATH" in str(excinfo.value)


def test_empty_command_is_a_programming_error():
    with pytest.raises(ValueError):
        wrap_command([])


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def test_probe_reports_outside_a_sandbox_when_the_kernel_accepts(monkeypatch):
    monkeypatch.setattr(
        claude_sandbox, "_sandbox_exec", lambda: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    assert already_sandboxed() is False


def test_probe_reports_inside_when_the_kernel_refuses(monkeypatch):
    """The refusal is the signal — an env marker would miss foreign sandboxes."""
    monkeypatch.setattr(
        claude_sandbox, "_sandbox_exec", lambda: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 71, "", "sandbox_apply: Operation not permitted",
        ),
    )
    assert already_sandboxed() is True


def test_probe_is_false_when_sandbox_exec_is_absent(monkeypatch):
    monkeypatch.setattr(claude_sandbox, "_sandbox_exec", lambda: None)
    assert already_sandboxed() is False


# ---------------------------------------------------------------------------
# describe() — report the truth, not the configuration
# ---------------------------------------------------------------------------


def test_describe_reports_unconfigured_honestly(monkeypatch):
    monkeypatch.setattr(
        claude_sandbox, "resolve_sandbox_profile_file", lambda: None,
    )
    report = describe()
    assert report["configured"] is False
    assert report["profile"] is None
    assert report["applicable"] is False


def test_describe_distinguishes_configured_from_working(monkeypatch, tmp_path):
    """The distinction the old wrapper's status command failed to make.

    A profile that exists but the kernel refuses must not read as
    "sandboxed".
    """
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _BROKEN)))
    monkeypatch.setattr(claude_sandbox, "_sandbox_exec", lambda: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 71, "", ""),
    )
    report = describe()
    assert report["configured"] is True
    assert report["applicable"] is False, (
        "configured is not the same claim as enforced"
    )


def test_describe_marks_unsupported_platforms(monkeypatch, tmp_path):
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", str(_write(tmp_path, _APPLICABLE)))
    monkeypatch.setattr(claude_sandbox.sys, "platform", "linux")
    report = describe()
    assert report["supported"] is False
    assert report["applicable"] is False


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_env_var_wins_over_the_config_dir(monkeypatch, tmp_path):
    from config_paths import resolve_sandbox_profile_file
    monkeypatch.setenv("PDT_SANDBOX_PROFILE", "/tmp/from-env.sb")
    assert resolve_sandbox_profile_file() == Path("/tmp/from-env.sb")


def test_tilde_in_the_env_var_is_expanded(monkeypatch, tmp_path):
    from config_paths import resolve_sandbox_profile_file
    monkeypatch.setenv(
        "PDT_SANDBOX_PROFILE", "~/somewhere/profile.sb",
    )
    resolved = resolve_sandbox_profile_file()
    assert str(resolved).startswith(str(Path.home()))
    assert "~" not in str(resolved)


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "profile.sb"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped example must stay applicable
# ---------------------------------------------------------------------------
#
# An example nobody can run is worse than no example: it is the first
# thing an operator copies, and a profile that fails to parse sends them
# looking for a Seatbelt bug instead of finding their own typo. This one
# failed exactly that way once — the header used ``#`` comments, borrowed
# from the YAML examples next to it, and Seatbelt reads ``#`` as a "sharp
# expression" and refuses the file.

EXAMPLE = BACKEND_DIR.parent / "example" / "sandbox_profile.sb.example"


@_REQUIRES_MACOS
def test_the_shipped_example_profile_applies():
    assert EXAMPLE.is_file(), f"missing {EXAMPLE}"
    proc = subprocess.run(
        ["sandbox-exec", "-f", str(EXAMPLE), "/usr/bin/true"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, (
        f"the example profile does not apply: "
        f"{(proc.stderr or proc.stdout).strip()[:200]}"
    )


def test_the_shipped_example_uses_seatbelt_comments():
    """``#`` is not a Seatbelt comment; ``;`` is."""
    body = EXAMPLE.read_text(encoding="utf-8")
    offenders = [
        line for line in body.splitlines()
        if line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"Seatbelt does not read '#' as a comment: {offenders[:2]}"
    )
