"""Per-round read-only context pack — one file holding a round's environment facts.

Why this module exists
----------------------
Every sub-agent that starts inside a worktree has to answer the same
eight questions before it can do anything useful: *where am I*, *which
branch*, *what is the base commit*, *what has changed*, *where is the plan
directory*, *which service ports matter*, *what is the focused test
command*. Today each of them answers those questions by exploring —
``ls``, ``find``, ``git log``, ``git status``, ``lsof`` — and the
exploration is repeated, because the sub-agent cannot see what the last
one found. Read-only exploration plus repeated reads account for 69% of
the tool calls in the largest execution sessions, while only a small
share of the calls actually run a test.

None of those eight facts needs a model to establish. They are local,
they are deterministic, and they are identical for every sub-agent in the
round. So this module collects them **once per round** into a small JSON
file, and the briefs hand out the path instead of the facts.

The contract
------------
* **Whitelist, not blacklist.** The writer serialises exactly
  :data:`CONTEXT_PACK_FIELDS` plus two metadata keys. It never copies the
  environment, never copies a configuration file, and never copies a
  process command line. A blacklist has to be extended every time a new
  secret-shaped value appears; a whitelist cannot leak what it does not
  name.

  ======================  ==================================================
  Field                   What it holds
  ======================  ==================================================
  ``worktree_abs_path``   The absolute path of the worktree the round runs
                          in.
  ``branch``              The branch checked out there; ``null`` on a
                          detached HEAD.
  ``base_commit``         The commit the branch left the trunk at — the
                          merge base of ``HEAD`` and the trunk.
  ``diff_stat``           ``git diff --stat`` between the base commit and
                          the working tree, so it covers the branch's
                          commits *and* the edits that are still
                          uncommitted.
  ``changed_files``       The paths that differ from the base commit, plus
                          the files that exist only in the working tree.
  ``plan_dir``            The plan's own directory.
  ``service_ports``       The ports this round cares about, each a port
                          number and a purpose label.
  ``focused_test_command``  The command this round is focused on.
  ======================  ==================================================

* **Missing is ``null``, not absent.** A reader can tell "this fact could
  not be collected" from "this fact was never asked for", and the key set
  of every pack is the same.
* **One file per round, never rewritten.** The path is
  ``<plan_dir>/context/round-<n>.json``. A second call for the same
  round returns the file that is already there rather than overwriting
  it, so a sub-agent reading the path cannot watch it change under its
  feet, and "which facts did this round run on" stays answerable.
* **Refuse before touching the disk.** A plan id that is not a single
  safe segment, or a plan directory that resolves outside the plans
  tree, raises :class:`ContextPackPathError` — and no directory is
  created on the way to the refusal.

The service-port fact is deliberately narrow: a port number and a
purpose label, never a configuration excerpt, a token, a cookie or a
command line. A command line can carry a secret; a program *name*
cannot, so the label is reduced to a safe character set before it is
written.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bounded_subprocess import run_bounded
from service_freshness import extract_ports_from_text
from utils.check_path import is_safe_dir_name

logger = logging.getLogger(__name__)

__all__ = [
    "CONTEXT_PACK_FIELDS",
    "SCHEMA_VERSION",
    "ContextPackError",
    "ContextPackPathError",
    "build_context_pack",
]


#: The eight environment facts a round runs on, in a fixed order.
#:
#: The order is part of the contract: the file lists the fields in this
#: order, so two packs can be compared by eye and a reader that knows the
#: list knows where to look.
CONTEXT_PACK_FIELDS = (
    "worktree_abs_path",
    "branch",
    "base_commit",
    "diff_stat",
    "changed_files",
    "plan_dir",
    "service_ports",
    "focused_test_command",
)

#: Bumped when the meaning of a field changes, so a consumer can tell two
#: generations of packs apart.
SCHEMA_VERSION = 1


class ContextPackError(Exception):
    """Raised when a context pack cannot be produced as asked."""


class ContextPackPathError(ContextPackError):
    """Raised when the pack's location is not safe to write.

    Attributes:
        reason: A short machine-readable reason, so a caller can map the
            refusal onto its own response without parsing the message.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------------------
# Credential shapes
# ---------------------------------------------------------------------------

#: What a redacted value is replaced with. It carries none of the shapes
#: the scan looks for, so a redacted file still scans clean.
_REDACTED = "[redacted]"

#: ``<name><separator><value>`` where the name mentions a credential.
#: The name part is matched loosely (``access_token``, ``--token``,
#: ``X-Api-Key``) because the separator is what makes it a credential.
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)"
    r"[A-Za-z0-9_.\-]*"
    r"(?:token|authorization|cookie|password|passwd|secret|apikey|api_key|"
    r"access_key|private_key|bearer)"
    r"[A-Za-z0-9_.\-]*"
    r"\s*[=:]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;)\]}]+)"
)

#: A bare mention of a header name, with no value to strip. The scan
#: forbids the word itself, so the word has to go.
_CREDENTIAL_BARE_WORD_RE = re.compile(
    r"(?i)\b(?:authorization|cookie)\b"
)

#: ``scheme://user:password@host`` — the userinfo part of a URL.
_CREDENTIAL_URL_USERINFO_RE = re.compile(r"(?i)(://)[^\s/:@]+:[^\s/@]+@")

#: A PEM private key block, header to footer.
_CREDENTIAL_PEM_RE = re.compile(
    r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)

#: Characters a service-port label may contain. Anything else — a space, a
#: quote, an equals sign — is dropped rather than escaped, so a label can
#: never carry a value.
_LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _redact(text: str) -> str:
    """Strip credential-shaped content out of one string.

    The whole match is replaced, not just its value: the scan the pack has
    to pass looks for ``token=`` and for the word ``Authorization``, so
    leaving the name behind would leave the hit behind with it.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _CREDENTIAL_PEM_RE.sub(_REDACTED, text)
    out = _CREDENTIAL_ASSIGNMENT_RE.sub(_REDACTED, out)
    out = _CREDENTIAL_URL_USERINFO_RE.sub(r"\1" + _REDACTED + "@", out)
    out = _CREDENTIAL_BARE_WORD_RE.sub(_REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    """Recursively redact every string inside a pack value."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validated_plan_id(plan_id: Any) -> str:
    """Return the plan id, or refuse it.

    A plan id names one directory inside the plans tree, so it has to be a
    single segment: no separator, no parent-directory token, no absolute
    path, no NUL byte. :func:`is_safe_dir_name` is the project's existing
    answer to that question, so this module does not re-derive it.
    """
    if not isinstance(plan_id, str) or not is_safe_dir_name(plan_id):
        raise ContextPackPathError(
            f"plan_id {plan_id!r} is not a single safe directory name",
            reason="plan_id is not a safe directory name",
        )
    return plan_id


def _validated_round_no(round_no: Any) -> int:
    """Return the round number, or refuse it.

    The number goes into a filename, so it has to be an integer: a string
    here would be a path traversal wearing a round's clothes.
    """
    if isinstance(round_no, bool) or not isinstance(round_no, int):
        raise ContextPackError(
            f"round_no must be an integer, got {round_no!r}"
        )
    if round_no < 0:
        raise ContextPackError(
            f"round_no must not be negative, got {round_no!r}"
        )
    return round_no


def _resolve_targets(
    plan_id: str, round_no: int, plan_dir: Path
) -> Tuple[Path, Path, Path]:
    """Return ``(context_dir, pack_path, resolved_plan_dir)``, or refuse.

    The refusal happens before anything is created, so a rejected plan id
    leaves no directory behind.
    """
    plan_dir = Path(plan_dir).expanduser()
    if not plan_dir.is_absolute():
        plan_dir = Path.cwd() / plan_dir

    if plan_dir.name != plan_id:
        raise ContextPackPathError(
            f"plan_dir {str(plan_dir)!r} is not the directory named by "
            f"plan_id {plan_id!r}",
            reason="plan_dir does not match plan_id",
        )

    plans_root = plan_dir.parent
    resolved_root = plans_root.resolve()
    if resolved_root == Path(resolved_root.anchor):
        raise ContextPackPathError(
            f"the plans directory {str(resolved_root)!r} is the filesystem root",
            reason="plans directory is the filesystem root",
        )

    resolved_plan = plan_dir.resolve()
    if resolved_root not in resolved_plan.parents:
        raise ContextPackPathError(
            f"plan_dir {str(plan_dir)!r} resolves to {str(resolved_plan)!r}, "
            f"which is outside the plans directory {str(resolved_root)!r}",
            reason="plan_dir escapes the plans directory",
        )

    context_dir = plan_dir / "context"
    target = context_dir / f"round-{round_no}.json"

    if target.is_symlink():
        raise ContextPackPathError(
            f"the context pack path {str(target)!r} is a symlink",
            reason="context pack path is a symlink",
        )

    resolved_target = target.resolve()
    if resolved_root not in resolved_target.parents:
        raise ContextPackPathError(
            f"the context pack would be written to {str(resolved_target)!r}, "
            f"which is outside the plans directory {str(resolved_root)!r}",
            reason="context pack escapes the plans directory",
        )

    return context_dir, target, resolved_plan


# ---------------------------------------------------------------------------
# Git facts
# ---------------------------------------------------------------------------

#: How long a single git query may take. A fact is not worth a hung
#: round: when the query does not come back, the field is ``null``.
_GIT_TIMEOUT_SECONDS = 10

#: The refs a branch's base commit is looked for, in order. A worktree
#: usually has the trunk only as a remote-tracking ref, and a repository
#: that has never fetched has it only as a local branch.
_BASE_REF_CANDIDATES = ("origin/main", "origin/master", "main", "master")


def _run_git(repo_dir: Path, args: Sequence[str]) -> Optional[str]:
    """Run one git command and return its stdout, or ``None``.

    ``None`` means "this fact is unavailable" — not a git repository, no
    such ref, a timeout. The caller turns that into ``null``.
    """
    try:
        completed = run_bounded(
            ["git", "-C", str(repo_dir), *args],
            shell=False,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a fact is best-effort
        logger.debug(
            "[context_pack] git %s failed in %s: %s", " ".join(args), repo_dir, exc
        )
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _git_branch(repo_dir: Path) -> Optional[str]:
    """The branch checked out in the worktree, or ``None``.

    A detached HEAD has no branch name, and an empty name would be a fact
    that reads like a value, so it becomes ``None``.
    """
    output = _run_git(repo_dir, ["branch", "--show-current"])
    if output is None:
        return None
    branch = output.strip()
    return branch or None


def _git_base_commit(repo_dir: Path) -> Optional[str]:
    """The commit this branch is based on.

    The merge base of ``HEAD`` and the trunk: the point where the round's
    branch left the main line. It is what makes "what has changed"
    answerable, so it is settled before the diff is taken.
    """
    for ref in _BASE_REF_CANDIDATES:
        if _run_git(repo_dir, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]) is None:
            continue
        merged = _run_git(repo_dir, ["merge-base", "HEAD", ref])
        if not merged:
            continue
        base = merged.strip()
        if base:
            return base
    return None


def _git_diff_facts(
    repo_dir: Path, base_commit: Optional[str]
) -> Tuple[Optional[str], Optional[List[str]]]:
    """The diff stat and the changed-file list, measured against the base.

    The comparison is base-to-working-tree, so it covers the commits the
    branch made *and* the edits that are still uncommitted. Files that
    exist only in the working tree are added from git's untracked list —
    a file nobody has staged yet is still a change the round made.

    With no base commit to measure against — a repository whose trunk
    cannot be resolved, or one with no commits at all — the comparison
    falls back to ``HEAD``, which answers a narrower question: what is
    uncommitted right now.
    """
    base_ref = base_commit or "HEAD"
    diff_stat = _run_git(repo_dir, ["diff", "--stat", base_ref])
    if diff_stat is None:
        return None, None

    names = _run_git(repo_dir, ["diff", "--name-only", base_ref])
    changed = [line.strip() for line in (names or "").splitlines() if line.strip()]

    untracked = _run_git(repo_dir, ["ls-files", "--others", "--exclude-standard"])
    for line in (untracked or "").splitlines():
        candidate = line.strip()
        if candidate and candidate not in changed:
            changed.append(candidate)

    return diff_stat.rstrip("\n"), sorted(changed)


# ---------------------------------------------------------------------------
# Service ports
# ---------------------------------------------------------------------------


def _run_probe(argv: Sequence[str]) -> Optional[str]:
    """Run a probe command and return its stdout, or ``None``.

    Best-effort by design: a machine without ``lsof`` has no listening
    ports to report, which is a missing fact, not a failed round.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - a probe is best-effort
        logger.debug("[context_pack] probe %s failed: %s", " ".join(argv), exc)
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _safe_label(text: str) -> str:
    """Reduce a program name to a label that cannot carry a value."""
    cleaned = _LABEL_SAFE_RE.sub("", (text or "").strip())
    return cleaned[:32]


def _port_from_socket_name(name: str) -> Optional[int]:
    """The port number in a socket name such as ``127.0.0.1:3000``."""
    text = (name or "").strip()
    if not text:
        return None
    try:
        port = int(text.rsplit(":", 1)[-1])
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _listening_ports() -> Dict[int, str]:
    """Every TCP port with a listener, with a label naming its program.

    Listener-only, for the same reason the freshness probe is: a socket
    in ``CLOSE_WAIT`` is not a service, and a machine full of closed
    connections would otherwise fill the pack with ports nobody serves.

    Only the program *name* is kept. A command line can carry a token; a
    program name cannot.
    """
    output = _run_probe(
        ["lsof", "-a", "-n", "-P", "-iTCP", "-sTCP:LISTEN", "-F", "cn"]
    )
    if not output:
        return {}

    ports: Dict[int, str] = {}
    program = ""
    for line in output.splitlines():
        if line.startswith("c"):
            program = _safe_label(line[1:])
        elif line.startswith("n"):
            port = _port_from_socket_name(line[1:])
            if port is not None:
                ports.setdefault(
                    port, f"listening:{program}" if program else "listening"
                )
    return ports


def _service_ports(focused_test_command: Optional[str]) -> List[Dict[str, Any]]:
    """The ports this round cares about.

    Two sources, one shape: the ports the focused test command names, and
    the ports something is already listening on. Each entry is a port
    number and a purpose label — the pair a reader needs to decide whether
    to talk to a service, and nothing that a service's configuration might
    have held.
    """
    purposes: Dict[int, str] = {}
    for port in sorted(extract_ports_from_text(focused_test_command or "")):
        purposes.setdefault(port, "focused_test_command")
    for port, label in _listening_ports().items():
        purposes.setdefault(port, label)
    return [{"port": port, "purpose": purposes[port]} for port in sorted(purposes)]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _write_pack_exclusive(path: Path, payload: Dict[str, Any]) -> bool:
    """Write the pack, never replacing one that is already there.

    Returns ``True`` when this call created the file and ``False`` when
    the file was already there. The write goes through a sibling
    temporary file and a hard link, so a reader never sees a half-written
    pack and a pack that already exists is never replaced.
    """
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    tmp_name: Optional[str] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return False
        except OSError:
            # A filesystem without hard links: create the file exclusively
            # instead, which still refuses to replace an existing one.
            try:
                target_fd = os.open(
                    path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                return False
            with os.fdopen(target_fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            return True
        return True
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - the temp file is already gone
                pass


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def build_context_pack(
    *,
    plan_id: str,
    round_no: int,
    plan_dir: Path,
    project_dir: Path,
    focused_test_command: str,
) -> Path:
    """Write this round's context pack and return its path.

    The pack lands at ``<plan_dir>/context/round-<n>.json`` and carries
    exactly :data:`CONTEXT_PACK_FIELDS` plus ``generated_at`` and
    ``schema_version``. A field that could not be collected is written as
    ``null``; the key is always there.

    Calling this twice for the same round is safe: the second call finds
    the file that is already there and returns it unchanged, so the pack
    stays the record of the facts the round actually ran on.

    Args:
        plan_id: The plan's id. It must name a single safe directory, and
            it must be the name of ``plan_dir``.
        round_no: The round number. It goes into the filename, so it has
            to be an integer.
        plan_dir: The plan's own directory, inside the plans tree.
        project_dir: The worktree the round runs in.
        focused_test_command: The command this round is focused on.

    Returns:
        The path of the context pack.

    Raises:
        ContextPackPathError: The id or the location is not safe to write.
        ContextPackError: The round number is not usable.
    """
    plan_id = _validated_plan_id(plan_id)
    round_no = _validated_round_no(round_no)

    plan_dir = Path(plan_dir).expanduser()
    project_dir = Path(project_dir).expanduser()
    context_dir, target, resolved_plan_dir = _resolve_targets(
        plan_id, round_no, plan_dir
    )

    if target.exists():
        # This round already has a pack. It is the record of what the
        # round was handed; rewriting it would change the past under a
        # sub-agent that is reading it.
        return target

    command = focused_test_command if isinstance(focused_test_command, str) else None
    if command is not None and not command.strip():
        command = None

    branch = _git_branch(project_dir)
    base_commit = _git_base_commit(project_dir)
    diff_stat, changed_files = _git_diff_facts(project_dir, base_commit)

    payload: Dict[str, Any] = {
        "worktree_abs_path": str(project_dir.resolve()),
        "branch": branch,
        "base_commit": base_commit,
        "diff_stat": diff_stat,
        "changed_files": changed_files,
        "plan_dir": str(resolved_plan_dir),
        "service_ports": _service_ports(command),
        "focused_test_command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
    }
    payload = {key: _redact_value(value) for key, value in payload.items()}

    context_dir.mkdir(parents=True, exist_ok=True)
    _write_pack_exclusive(target, payload)
    return target
