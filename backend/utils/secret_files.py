"""Private on-disk handling for payloads that carry provider credentials.

Why this module exists
----------------------
The subagent settings file carries provider credentials in its ``env``
block (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` — the *routed*
provider's key, which is what makes per-provider routing work, so it
cannot simply be dropped). It was written to a flat
``/tmp/subagent_settings_<uuid>.json`` with the default file mode, and
never removed.

``/tmp`` is mode ``1777``: the sticky bit stops other local accounts
*deleting* your files, but not *reading* them, and a ``0644`` file is
readable by every account on the box. The name is discoverable too —
``claude --settings <path>`` puts the path in the process table, and the
path is handed to the subagent itself as ``CLAUDE_SETTINGS_PATH``, so a
subagent does not even have to guess it.

Nothing removes them, so the population is not bounded by any single run:
every dispatch leaves one more readable copy of a live credential behind,
and the set only grows.

Three properties fix it. They are independent layers — any one alone
leaves a gap:

* :func:`private_dir` — ``mkdtemp`` gives a ``0700`` directory under a
  per-user root (``~/.pdt-scratch``). Two reasons that root is not the
  system temp directory. On Linux ``gettempdir()`` *is* ``/tmp``
  (``1777``), so only the directory ``mkdtemp`` creates is private. And
  the temp root is a different string on every platform (``/tmp`` on
  Linux, ``/var/folders/…/T`` on macOS, and a ``TMPDIR`` that the guard's
  own subprocess may not even inherit), so "go look in your temp
  directory" is not a path an operator can act on.
* :func:`write_private_json` — ``mkdtemp`` sets the *directory* mode, not
  the file's. A file created inside it is still ``0644`` under the
  default umask, so the file mode has to be set explicitly (and forced
  after creation, since a permissive umask would otherwise leak bits).
* :func:`redact` — the credential is only needed while the child runs.
  The file is kept afterwards for post-mortem debugging (what an operator
  reads is the hook-config layout, not the key), so the secret is
  replaced rather than the file deleted.

Timing matters. ``coding_tool_hooks/pre_tool_use.sh`` reads
``$CLAUDE_SETTINGS_PATH`` on *every* tool call to log
``ANTHROPIC_BASE_URL``, so redaction may only run once the child has been
reaped. A crash in between leaves the secret on disk — which is exactly
why the 0700/0600 layers above are not optional, and why they are applied
at write time rather than at cleanup time.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: The ``env`` keys whose values are credentials rather than routing
#: configuration. ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_MODEL`` are
#: deliberately absent: they are not secrets, and they are the single
#: most useful thing in the file when debugging a routing mistake.
SENSITIVE_ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: What replaces a credential in the post-mortem copy. Chosen to be
#: obviously not-a-key so a reader never mistakes it for a live one.
REDACTED = "<redacted>"

#: Directory prefix for :func:`private_dir`. Greppable in ``ls -d
#: $TMPDIR/pdt-*`` when an operator needs to find the residue.
DIR_PREFIX = "pdt-subagent-"

#: File mode for anything written by this module.
PRIVATE_FILE_MODE = 0o600

#: Name of a payload a **separate program** writes directly into a temp
#: root rather than into a :func:`private_dir` directory — the operator
#: fleet's nightly runner, which cannot import this module across repos.
#:
#: Matched by exact shape (the writer's own literal prefix plus the hex of
#: a ``uuid4``) and never by prefix alone. The only reason to rewrite a
#: file outside a directory this module created is that we know precisely
#: which file it is; ``nightly_ci_settings_`` followed by anything at all
#: would be a guess, and a guess is what :func:`is_managed` exists to
#: refuse.
FLAT_TEMP_SETTINGS_RE = re.compile(r"^nightly_ci_settings_[0-9a-f]{32}\.json$")

#: Root :func:`private_dir` mints its directories under, overriding the
#: per-user default when set.
#:
#: A dispatch is a short-lived thing, but the directories it leaves are
#: not: the boot-time sweep rewrites the *credentials* in a payload and
#: keeps the directory for post-mortem reading, and the empty-directory
#: prune cannot touch a directory that still holds that payload. So under
#: a harness that dispatches constantly the default root grows by one
#: directory per dispatch and nothing collects it.
#:
#: This override is how a harness keeps that scratch inside something it
#: owns and removes. Same shape as ``PDT_PLANS_DIR`` /
#: ``PDT_STATE_DB_PATH`` / ``PDT_LOCK_ROOT``: the default is a per-user
#: directory the operator owns, and the suite points it at its own
#: session directory.
PRIVATE_ROOT_ENV_VAR = "PDT_SECRET_TEMP_ROOT"

#: Where the private directories go when :data:`PRIVATE_ROOT_ENV_VAR` is
#: unset — a dotted name under the user's home, not a temp directory.
#:
#: Home, because it is the one location with the same meaning on every
#: platform: ``/tmp`` is ``/tmp`` on Linux and a symlink to
#: ``/private/tmp`` on macOS, and ``TMPDIR`` is a per-process value on
#: both — so "go look in your temp directory" is not an instruction an
#: operator can paste anywhere.
#:
#: The cost is that nothing collects it. The OS swept the temp root on a
#: timer (macOS ``tmp_cleaner``, Linux ``systemd-tmpfiles``); nothing
#: sweeps a home directory. :data:`PRIVATE_ROOT_ENV_VAR` is the way out
#: of that cost for a harness that dispatches constantly: point the root
#: at a session directory and remove it with the session.
DEFAULT_PRIVATE_ROOT_NAME = ".pdt-scratch"


def default_private_root() -> Optional[Path]:
    """``~/.pdt-scratch``, or ``None`` when the home dir is unknown.

    Public because the boot-time sweep has to look in the same place.
    Naming the path in two modules is how they drift apart, and a sweep
    pointed at the wrong root is a sweep that finds nothing — which reads
    exactly like "this machine is clean".
    """
    try:
        return Path.home() / DEFAULT_PRIVATE_ROOT_NAME
    except (RuntimeError, OSError):
        # No resolvable home: a service account with no passwd entry, or a
        # container started without HOME. The caller falls back to the
        # system temp root — worse, because nothing collects it, but the
        # 0700/0600 layers still hold.
        return None


def _private_root() -> Optional[Path]:
    """The override if one is set, else the per-user default.

    Read on each call rather than cached at import, so a harness that
    sets the variable in a fixture still takes effect for code imported
    earlier. ``None`` means "no root at all" and the caller lets
    ``mkdtemp`` choose the system temp root itself.
    """
    override = os.environ.get(PRIVATE_ROOT_ENV_VAR, "").strip()
    return Path(override) if override else default_private_root()


def current_private_root() -> Optional[Path]:
    """The root :func:`private_dir` will use right now, or ``None``.

    Public view of :func:`_private_root` for the boot-time sweep. It
    exists so the writer and the sweeper cannot name the root
    differently — a sweep pointed at a root nothing writes to reports a
    clean machine, which is the one failure mode a cleanup tool must not
    have.
    """
    return _private_root()


def private_dir(prefix: str = DIR_PREFIX) -> Path:
    """Create a fresh ``0700`` directory under the private root.

    Deliberately **not** inside the workspace: these files belong to a
    single dispatch, and dropping them into the project directory would
    pollute the delivered tree (the attributable-diff gate would flag
    them too).

    ``mkdtemp`` is used rather than the root itself because the root may
    be a shared directory (``/tmp`` on Linux is ``1777``) — only the
    directory ``mkdtemp`` creates is private, whatever the root's mode.

    The root is ``~/.pdt-scratch`` unless :data:`PRIVATE_ROOT_ENV_VAR`
    names another one (see that constant for why a harness wants that).
    An override that does not exist yet is created ``0700`` rather than
    left to ``mkdtemp``, which would fail on a missing ``dir``.
    """
    root = _private_root()
    if root is None:
        return Path(tempfile.mkdtemp(prefix=prefix))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write ``payload`` to ``path`` with mode ``0600``.

    Atomic (tmpfile + ``os.replace``) so a concurrent reader never sees a
    half-written document. ``os.replace`` carries the *tmpfile's* mode
    across, so setting it on the tmpfile is what makes the final file
    private.

    The mode is both passed to ``os.open`` and forced with a follow-up
    ``os.chmod`` — ``os.open``'s mode argument is filtered through the
    process umask, and a permissive umask is exactly the environment
    where this matters.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, PRIVATE_FILE_MODE)
    os.replace(tmp, path)
    os.chmod(path, PRIVATE_FILE_MODE)


def is_managed(path: Optional[Path]) -> bool:
    """True when ``path`` lives in a directory :func:`private_dir` created.

    **This guard is load-bearing.** ``coding_tool`` reads whatever
    ``self.settings`` points at, and in some configurations that is the
    operator's own ``~/.claude/settings.json`` rather than one of our
    tempfiles. Redacting on the way out would then rewrite the user's
    configuration in place — replacing their key with ``<redacted>`` —
    which is a far worse outcome than the leak this module exists to
    close. Only files inside directories we created are eligible.

    The check is on the *directory* name, not the file name, because the
    directory is the thing this module controls; a caller cannot get a
    file into a ``pdt-subagent-*`` directory except through
    :func:`private_dir`.
    """
    if path is None:
        return False
    try:
        return Path(path).parent.name.startswith(DIR_PREFIX)
    except (OSError, ValueError):
        return False


def is_flat_temp_settings(path: Optional[Path],
                          roots: Iterable[Path]) -> bool:
    """True when ``path`` is a flat-temp payload of ours under ``roots``.

    The narrower half of the eligibility question: a file whose name
    matches :data:`FLAT_TEMP_SETTINGS_RE` **and** whose parent *is* one of
    ``roots`` (directly, not somewhere below it). Both halves are
    required — the name alone would match a file an attacker planted in a
    world-writable temp root, and the location alone matches everything
    every program on the machine leaves in ``/tmp``.

    Comparison is on the real path of the parent, against the real path of
    each root, so a symlinked temp root (``/tmp`` on macOS) still matches
    the directory the caller actually scanned.
    """
    if path is None:
        return False
    candidate = Path(path)
    if not FLAT_TEMP_SETTINGS_RE.match(candidate.name):
        return False
    try:
        parent = os.path.realpath(candidate.parent)
    except OSError:
        return False
    for root in roots or ():
        try:
            if os.path.realpath(root) == parent:
                return True
        except OSError:
            continue
    return False


def redact(path: Optional[Path], *, logger: Any = None,
           flat_temp_roots: Iterable[Path] = ()) -> bool:
    """Replace credential values in ``path`` with :data:`REDACTED`.

    Called once the child process has been reaped. Returns ``True`` when
    the file was rewritten, ``False`` when there was nothing to do, the
    path is not one of ours (:func:`is_managed`), or the rewrite failed —
    a failure is never raised, because losing the post-mortem copy must
    not fail a task that has already finished.

    ``flat_temp_roots`` widens eligibility by exactly one shape: a
    payload written *directly* into one of those roots rather than into a
    :func:`private_dir` directory, named per
    :data:`FLAT_TEMP_SETTINGS_RE`. It defaults to empty, so the dispatch
    call site in ``coding_tool`` is unaffected and a caller cannot widen
    the guard by passing a flag — it has to name the roots it scanned,
    and :func:`is_flat_temp_settings` re-checks the name and the location
    against them.

    On an unreadable or non-JSON file nothing is changed: the credential
    cannot be located, so the ``0600``/``0700`` layers remain the
    protection. That path is logged at WARNING so an operator can see the
    file that needs attention.
    """
    if not (is_managed(path) or is_flat_temp_settings(path, flat_temp_roots)):
        return False
    path = Path(path)
    try:
        if not path.exists():
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _warn(logger, f"could not read {path} for redaction: "
                      f"{type(exc).__name__}: {exc}")
        return False

    if not isinstance(raw, dict):
        _warn(logger, f"settings payload in {path} is "
                      f"{type(raw).__name__}, not an object; left as-is")
        return False

    env = raw.get("env")
    if not isinstance(env, dict):
        return False

    changed = False
    for key in SENSITIVE_ENV_KEYS:
        value = env.get(key)
        # ``value != REDACTED`` matters: the placeholder is a non-empty
        # string, so without it a second pass would rewrite the file
        # again and report a change that did not happen — making the
        # caller's rewritten-count meaningless.
        if value and value != REDACTED:
            env[key] = REDACTED
            changed = True
    if not changed:
        return False

    try:
        write_private_json(path, raw)
    except OSError as exc:
        _warn(logger, f"could not redact {path}: "
                      f"{type(exc).__name__}: {exc}")
        return False
    return True


def redact_all(paths: Iterable[Optional[Path]], *, logger: Any = None) -> int:
    """Redact every path in ``paths``; return how many were rewritten.

    Duplicates are collapsed — the two writers can hand the same path
    twice when the caller reuses one settings file.
    """
    seen: List[str] = []
    count = 0
    for p in paths:
        if p is None:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.append(key)
        if redact(Path(p), logger=logger):
            count += 1
    return count


def _warn(logger: Any, message: str) -> None:
    """Log without assuming the caller's logger shape.

    ``logger`` here is either the backend's event logger (has ``.warning``)
    or a stdlib logger (same) or ``None``. Kept local so this module stays
    a stdlib-only leaf.
    """
    if logger is None:
        return
    try:
        logger.warning(message)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass
