"""Run coding sub-agents under an OS sandbox.

2026-10-06. A plan's sub-agents were spawned as::

    claude --verbose --output-format stream-json --permission-mode bypassPermissions

with no confinement of any kind: ``bypassPermissions`` removes the
confirmation prompts, and nothing below it constrained the filesystem.
An operator can point this module at a Seatbelt profile and every
spawned agent runs inside it.

Why the failure mode is *loud*
--------------------------------
A sandbox that silently turns itself off is worse than no sandbox,
because the operator's configuration says it is on. That is not
hypothetical — it is what happened here: a machine-wide ``~/bin/claude``
symlink once routed every invocation through a wrapper, the wrapper's
marker file was absent, and ``/sandbox`` cheerfully reported "ON
(sandboxed)" while nothing was enforcing anything. A plan then failed
at task 1 with eleven consecutive::

    sandbox-exec: sandbox_apply: Operation not permitted

So the contract here is deliberately asymmetric:

* **Not configured** → spawn unsandboxed, exactly as before. Nothing
  about a default install changes.
* **Configured and applicable** → spawn under the profile.
* **Configured but not applicable** → :class:`SandboxUnavailableError`.
  No fallback, no warning-then-continue. A configured sandbox that
  cannot be applied is a configuration error, and running the agent
  anyway is exactly the silent degradation described above.

Nesting
--------
macOS does not accept an arbitrary nested Seatbelt profile, and a
sub-agent is often already inside one — PDT itself may have been
launched from a session that is sandboxed. Rather than guess, this
module *tries* the wrap and reports what happened:

* If the profile can be applied, it is applied. Identical profiles
  nest fine, so a deployment that points PDT at the same profile its
  launcher uses works with no special handling.
* If it cannot, the error names the likely cause. The commonest one is
  that PDT is running inside a *different* sandbox; the fix is to
  point ``PDT_SANDBOX_PROFILE`` at that same profile.

Non-macOS
---------
``sandbox-exec`` is a macOS facility. A profile configured on Linux or
Windows is a configuration error here, not something to ignore —
consistent with the fail-closed contract above.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config_paths import resolve_sandbox_profile_file

#: Environment variable the wrapped launcher sets, so a nested PDT run
#: (or a human debugging one) can tell that confinement is in effect and
#: which profile produced it.
ACTIVE_ENV = "PDT_SANDBOX_ACTIVE"

#: A deliberately unrestricted profile, used only to ask the kernel "am I
#: already inside a sandbox?". The kernel refuses to apply it when there
#: is an enclosing profile that does not contain it, which is precisely
#: the question. It is never used to confine anything.
_PROBE_PROFILE = "(version 1)\n(allow default)\n"


class SandboxUnavailableError(RuntimeError):
    """A sandbox was configured but cannot be applied.

    Raised instead of falling back to an unsandboxed spawn. See the
    module docstring for why the fallback is the worse failure.
    """


@dataclass(frozen=True)
class SandboxDecision:
    """What :func:`wrap_command` did, and why.

    ``command`` is the list to execute; ``reason`` is a short stable
    token suitable for a log field or a test assertion, and ``detail``
    carries the operator-facing sentence.
    """

    command: List[str]
    profile: Optional[Path] = None
    reason: str = "not_configured"
    detail: str = ""

    @property
    def sandboxed(self) -> bool:
        return self.profile is not None and self.reason in ("applied", "inherited")


def _sandbox_exec() -> Optional[str]:
    return shutil.which("sandbox-exec")


def already_sandboxed() -> bool:
    """True when this process is already inside an OS sandbox.

    Asks the kernel directly by attempting to apply an unrestricted
    profile. An env-var marker would be cheaper but only catches the
    nesting this project creates; the failure being debugged involved a
    sandbox set up by something else, and a marker we do not set would
    not have seen it.
    """
    exe = _sandbox_exec()
    if exe is None:
        return False
    try:
        proc = subprocess.run(
            [exe, "-f", "/dev/stdin", "/usr/bin/true"],
            input=_PROBE_PROFILE,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode != 0


def _describe_failure(profile: Path) -> str:
    """Turn "it did not work" into a sentence the operator can act on."""
    if sys.platform != "darwin":
        return (
            f"PDT_SANDBOX_PROFILE points at {profile}, but sandbox-exec "
            f"is a macOS facility and this process is running on "
            f"{sys.platform!r}. Unset the variable to run unsandboxed, or "
            f"configure a per-platform sandbox mechanism."
        )
    if _sandbox_exec() is None:
        return (
            f"PDT_SANDBOX_PROFILE points at {profile}, but sandbox-exec is "
            f"not on PATH. Unset the variable to run unsandboxed."
        )
    if already_sandboxed():
        return (
            f"PDT_SANDBOX_PROFILE points at {profile}, but this process is "
            f"already inside an OS sandbox, and macOS only accepts a nested "
            f"profile that the enclosing one contains. Point "
            f"PDT_SANDBOX_PROFILE at the profile the enclosing sandbox was "
            f"started with — an identical profile nests fine — or unset it "
            f"to inherit the enclosing sandbox."
        )
    return (
        f"PDT_SANDBOX_PROFILE points at {profile}, which the kernel refused "
        f"to apply. Check that the file exists and parses as a Seatbelt "
        f"profile (`sandbox-exec -f {profile} /usr/bin/true` reproduces it)."
    )


def _validate(profile: Path) -> None:
    if not profile.is_file():
        raise SandboxUnavailableError(
            f"PDT_SANDBOX_PROFILE points at {profile}, which does not exist."
        )
    if sys.platform != "darwin":
        raise SandboxUnavailableError(_describe_failure(profile))
    if _sandbox_exec() is None:
        raise SandboxUnavailableError(_describe_failure(profile))


def wrap_command(
    command: Sequence[str],
    env: Optional[Dict[str, str]] = None,
) -> SandboxDecision:
    """Return ``command`` wrapped in the configured sandbox, if any.

    Args:
        command: the argv to run, Claude first.
        env: the environment the child will receive. When a sandbox is
            applied it gains :data:`ACTIVE_ENV` so descendants can tell
            they are confined. The caller's dict is not mutated.

    Returns:
        A :class:`SandboxDecision`. ``reason`` is one of
        ``not_configured``, ``applied``.

    Raises:
        SandboxUnavailableError: a profile is configured but cannot be
            applied. Deliberately not caught and degraded into an
            unsandboxed spawn.
    """
    argv = list(command)
    if not argv:
        raise ValueError("wrap_command() needs a non-empty command")

    profile = resolve_sandbox_profile_file()
    if profile is None:
        return SandboxDecision(
            command=argv, reason="not_configured",
            detail="no sandbox configured; spawning unsandboxed",
        )

    _validate(profile)
    exe = _sandbox_exec()
    wrapped = [str(exe), "-f", str(profile), "--", *argv]

    # Prove it applies before handing the argv to Popen. A refusal here
    # is the kernel's, and it is the same refusal that stopped a plan at
    # task 1 on 2026-10-04 — surfacing it as a configuration error is
    # what keeps it from recurring invisibly.
    try:
        probe = subprocess.run(
            [*wrapped[:3], "/usr/bin/true"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailableError(
            f"could not run sandbox-exec to validate {profile}: {exc}"
        ) from exc
    if probe.returncode != 0:
        raise SandboxUnavailableError(
            f"{_describe_failure(profile)} (sandbox-exec said: "
            f"{(probe.stderr or probe.stdout or '').strip()[:200]})"
        )

    if env is not None:
        env[ACTIVE_ENV] = str(profile)
    return SandboxDecision(
        command=wrapped, profile=profile, reason="applied",
        detail=f"sandboxed under {profile}",
    )


def describe() -> Dict[str, Any]:
    """A JSON-friendly summary for the status endpoint and logs.

    Reports the truth an operator needs — is it configured, does it
    apply, is this process already confined — rather than echoing the
    environment variable back at them, which is how the old machine-wide
    wrapper ended up reporting "sandboxed" while enforcing nothing.
    """
    profile = resolve_sandbox_profile_file()
    out: Dict[str, Any] = {
        "configured": profile is not None,
        "profile": str(profile) if profile else None,
        "platform": sys.platform,
        "supported": sys.platform == "darwin",
        "already_sandboxed": already_sandboxed(),
        "applicable": False,
    }
    if profile is not None and out["supported"] and profile.is_file():
        if _sandbox_exec() is None:
            return out
        try:
            probe = subprocess.run(
                [_sandbox_exec(), "-f", str(profile), "/usr/bin/true"],
                capture_output=True, text=True, timeout=10,
            )
            out["applicable"] = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            out["applicable"] = False
    return out
