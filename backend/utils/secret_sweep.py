"""Collect the credential-bearing settings payloads a crash left behind.

Why this module exists
----------------------
``utils.secret_files`` redacts a dispatch's ``--settings`` payload once
the child is reaped, and that covers every way a dispatch can *end*. It
cannot cover the ways the **backend** ends: a killed, crashed, or
restarted process never reaches its own cleanup, so whatever was in
flight keeps a live credential on disk for as long as the file sits in
the temp root — indefinitely, because nothing else collects it.

``scripts/redact_leaked_secrets.py`` has always been able to do that
collection; what it never had was a caller. It was a command an operator
had to remember, so the residue was bounded by operator memory rather
than by any run. This module is the same sweep with an importable entry
point, so the backend's startup path can run it.

The walk itself is unchanged and its safety properties still hold:

* **Dry run by default.** ``apply`` must be passed explicitly.
* **Never touches a file a live process is using.** Paths appearing
  after ``--settings`` in the process table are skipped; redacting one
  out from under a running subagent hands it ``<redacted>`` as its API
  key.
* **Never follows a symlink.** A symlink named like a settings file,
  planted in a world-writable temp root, would otherwise redirect the
  rewrite at a file of the attacker's choosing.
* **Deletes directories only, and only under two named gates.**
  ``prune_empty_dirs`` removes an empty ``pdt-subagent-*`` /
  ``pdt-ws-locks-*`` directory older than a settle window, so it cannot
  race a dispatch that has made the directory but not yet written the
  file. ``prune_aged_residue`` removes a ``pdt-subagent-*`` directory
  older than a much wider gate **whatever it holds** — a dispatch's
  post-mortem copy stops being worth keeping long before that, and a
  directory whose mtime is that old cannot belong to a child that is
  still running. No file is ever removed by either.

Where the callers live
----------------------
* ``scripts/redact_leaked_secrets.py`` — the operator CLI.
* ``backend/server.py`` — one sweep per boot. Booting is the one moment
  when nothing of ours can legitimately be running, which is what makes
  it the safe place to clean up after a previous life; the startup
  service-orphan sweep next to it runs for the same reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from utils.secret_files import (
    DIR_PREFIX,
    FLAT_TEMP_SETTINGS_RE,
    REDACTED,
    SENSITIVE_ENV_KEYS,
    is_flat_temp_settings,
    is_managed,
    redact,
)

#: File-name patterns this project's writers generate into the temp root.
#: Matched by stem prefix + ``.json`` suffix rather than by a glob, so the
#: match is explicit and testable.
#:
#: ``nightly_ci_settings_`` is the operator fleet's nightly runner, which
#: is a separate program and so has to be listed here by name — a sweep
#: that only knows the names *this* repo writes cannot see it.
_GENERATED_STEMS = (
    "subagent_settings_",
    "verif_settings_",
    "nightly_ci_settings_",
)

#: How far below each root to look. ``secret_files.private_dir()`` puts
#: one directory under the root and the file inside it, so 2 is the whole
#: shape. A deeper walk would only find things that are not ours.
_SCAN_DEPTH = 2

#: ``--settings <path>`` in a process command line. Both the space form
#: and the ``=`` form are accepted; quotes are stripped before comparison.
_PS_SETTINGS_RE = re.compile(r"""--settings[=\s]+("[^"]*"|'[^']*'|\S+)""")

#: Skip-list: a symlink is not a file we wrote.
_SYMLINK = "symlink"

#: Set to a non-empty value to make :func:`sweep_default_roots` a no-op.
#:
#: The sweep is a **machine-wide** rewrite: it walks the real temp roots
#: and overwrites credentials in files it recognises. That is the point in
#: production — booting is the one moment nothing of ours can legitimately
#: be running — and it is exactly what must not happen because a test
#: booted the app. Any test that enters ``server._lifespan`` (directly, or
#: through ``TestClient``'s context manager) would otherwise rewrite the
#: developer's own residue as a side effect of running the suite.
#:
#: Same mechanism as ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``, for the
#: same reason: the alternative is every such test remembering to stub it.
DISABLE_ENV = "PDT_DISABLE_TEMP_SWEEP"


def default_roots() -> List[Path]:
    """Temp roots to scan, deduplicated by real path.

    ``tempfile.gettempdir()`` is the root the private-directory writers
    use, but it is not ``/tmp`` on macOS — and a writer that hardcoded
    ``/tmp`` put its payload there instead. Both are scanned so a machine
    that saw either version gets cleaned by one call.
    """
    candidates = [Path(os.environ.get("TMPDIR") or "/tmp"), Path("/tmp")]
    try:
        import tempfile

        candidates.insert(0, Path(tempfile.gettempdir()))
    except Exception:  # noqa: BLE001 - gettempdir is best-effort
        pass

    seen: Dict[str, Path] = {}
    for c in candidates:
        try:
            real = Path(os.path.realpath(c))
        except OSError:
            continue
        if real.is_dir():
            seen.setdefault(str(real), real)
    return list(seen.values())


def is_generated_settings_name(name: str) -> bool:
    """True for a file name this project's writers generate."""
    return name.endswith(".json") and name.startswith(_GENERATED_STEMS)


def _is_candidate_name(name: str, depth: int) -> bool:
    """Whether a directory entry at ``depth`` is one of our payloads.

    Two shapes, and the asymmetry between them is deliberate.

    A file one directory below a root is the shape
    ``secret_files.private_dir()`` produces, and there the loose stem
    match is right: the enclosing ``pdt-subagent-*`` directory is itself
    evidence the file is ours.

    A file *directly* in a root has no such enclosure — it is one of
    thousands of unrelated files every program on the machine leaves in a
    world-writable temp root. So only the exact flat-temp name counts,
    which is the same pattern :func:`is_flat_temp_settings` re-checks
    before any rewrite. A stem match here would put every
    ``<prefix>anything.json`` in ``/tmp`` in the refusal report.
    """
    if depth == _SCAN_DEPTH:
        return is_generated_settings_name(name)
    if depth == 1:
        return bool(FLAT_TEMP_SETTINGS_RE.match(name))
    return False


def find_candidates(roots: Iterable[Path]) -> List[Path]:
    """Every generated-settings file at or below ``roots``, sorted.

    Uses ``os.scandir`` rather than ``Path.rglob`` so a directory the
    account cannot read (macOS ``TemporaryItems``) is skipped instead of
    aborting the walk.
    """
    found: List[Path] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > _SCAN_DEPTH:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    # Still surface it if the *name* matches — a planted
                    # symlink is worth reporting, just not following.
                    if _is_candidate_name(entry.name, depth):
                        found.append(Path(entry.path))
                    continue
                if entry.is_dir(follow_symlinks=False):
                    _walk(Path(entry.path), depth + 1)
                elif _is_candidate_name(entry.name, depth):
                    found.append(Path(entry.path))
            except OSError:
                continue

    for root in roots:
        _walk(Path(root), 1)
    return sorted(set(found))


def settings_paths_in_use() -> set:
    """Real paths currently named after ``--settings`` in the process table.

    Best-effort. A false positive only makes the caller skip a file (it
    will be caught on the next run); a false negative would redact a live
    subagent's key, so the regex is deliberately loose.
    """
    try:
        proc = subprocess.run(
            ["ps", "-Awwo", "command="],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    in_use = set()
    for match in _PS_SETTINGS_RE.finditer(proc.stdout or ""):
        raw = match.group(1).strip("\"'")
        if not raw:
            continue
        try:
            in_use.add(os.path.realpath(raw))
        except OSError:
            in_use.add(raw)
    return in_use


def live_credential_keys(path: Path) -> Optional[List[str]]:
    """``env`` keys still holding a live credential, or ``None`` if unreadable.

    An empty string counts as *not* live: ``SubagentConfig`` emits
    ``ANTHROPIC_AUTH_TOKEN`` unconditionally and it is legitimately blank
    for a provider that only uses an API key.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    env = raw.get("env")
    if not isinstance(env, dict):
        return []
    live = []
    for key in SENSITIVE_ENV_KEYS:
        value = env.get(key)
        if value and value != REDACTED:
            live.append(key)
    return live


class Finding:
    """One candidate file and what the sweep decided about it."""

    def __init__(self, path: Path, status: str, detail: str = "",
                 keys: Optional[Sequence[str]] = None) -> None:
        self.path = path
        self.status = status
        self.detail = detail
        self.keys = list(keys or [])

    @property
    def needs_action(self) -> bool:
        return self.status == "live"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "path": str(self.path),
            "status": self.status,
            "detail": self.detail,
            "keys": self.keys,
        }
        try:
            out["mtime"] = int(self.path.stat().st_mtime)
        except OSError:
            pass
        return out


def classify(path: Path, in_use: set, roots: Sequence[Path]) -> Finding:
    """Decide what to do with one candidate, without touching it."""
    try:
        if path.is_symlink():
            return Finding(path, _SYMLINK,
                           "refused: symlinks are never followed")
    except OSError as exc:
        return Finding(path, "unreadable", f"{type(exc).__name__}: {exc}")

    try:
        real = os.path.realpath(path)
    except OSError:
        real = str(path)
    if real in in_use:
        return Finding(path, "in_use",
                       "a live process has this path after --settings")

    # ``roots`` is what separates the two eligible shapes: a payload
    # inside a directory ``private_dir()`` created, or a flat-temp payload
    # whose name and parent match exactly. Anything else is refused — the
    # guard that keeps an operator's own ``~/.claude/settings.json`` from
    # being rewritten in place.
    if not (is_managed(path) or is_flat_temp_settings(path, roots)):
        return Finding(path, "unmanaged",
                       "not in a pdt-subagent-* directory and not a "
                       "recognised flat-temp payload; refusing to rewrite "
                       "a file this project did not create")

    keys = live_credential_keys(path)
    if keys is None:
        return Finding(path, "unreadable", "not valid JSON")
    if not keys:
        return Finding(path, "clean", "no live credential")
    return Finding(path, "live", "holds a live credential", keys)


#: Directory prefixes this sweep may remove once they are empty and past
#: the age gate.
#:
#: ``DIR_PREFIX`` is the private directory a settings payload is written
#: into. Removing it is what bounds the population: the redaction keeps
#: the *file* for post-mortem, so a directory that ever held one is never
#: empty — but a directory created for a dispatch that then failed before
#: writing is, and those are the ones that accumulate without limit.
#:
#: ``pdt-ws-locks-`` is the fallback lock root a workspace without a plan
#: directory gets (``file_lock_protocol.fallback_locks_dir``). It is keyed
#: by workspace digest, so a test suite that gives every case its own
#: ``tmp_path`` mints one per case. Its writer creates the directory with
#: ``mkdir(parents=True, exist_ok=True)``, so removing an empty one cannot
#: break a later writer — which is what makes it eligible here at all.
#:
#: Empty-and-old is the whole safety argument in both cases: a directory
#: with anything in it is left alone, so nothing a live process is using
#: is ever a candidate.
_PRUNABLE_DIR_PREFIXES = (DIR_PREFIX, "pdt-ws-locks-")

#: Age gate for the boot-time prune. A directory younger than this may
#: belong to a dispatch that has created it and not yet written into it,
#: so it is left for the next boot to judge.
DEFAULT_PRUNE_MIN_AGE_SEC = 600.0

#: Prefixes the *age* rule may remove even when they are not empty.
#:
#: Deliberately narrower than :data:`_PRUNABLE_DIR_PREFIXES`, and the
#: omission is the interesting half. ``pdt-subagent-*`` is keyed to a
#: single dispatch: the child lives for minutes, the redaction runs once
#: it is reaped, and nothing reads the file again. So the directory's
#: mtime *is* "when this stopped being used", and an old one cannot belong
#: to a child that is still running.
#:
#: ``pdt-ws-locks-*`` does not have that property. Its mtime records when
#: the directory was *made*, not when it was last used — the writer's
#: ``mkdir(exist_ok=True)`` only touches the directory when the first lock
#: file in it is created. A workspace in constant use all year can
#: therefore sit in a directory whose mtime is a year old, and removing
#: that directory out from under a process holding a lock **inside** it
#: would break exclusion silently: the next process makes a fresh
#: directory, finds no lock in it, and proceeds alongside the holder. The
#: lock population is also bounded by the number of workspaces the machine
#: has run against, not by dispatch count, so there is nothing here that
#: needs bounding.
_AGED_PRUNABLE_DIR_PREFIXES = (DIR_PREFIX,)

#: Age past which a ``pdt-subagent-*`` directory is removed whatever it
#: holds.
#:
#: Wide on purpose. The directory holds the redacted payload kept for
#: post-mortem reading, and post-mortem value decays in days — but the
#: gate only has to be wide enough that it can never plausibly describe a
#: live dispatch, and a generous margin costs nothing except disk the
#: machine was not going to look at again. Three months is that margin.
DEFAULT_RESIDUE_MAX_AGE_SEC = 90 * 24 * 60 * 60.0


def prune_empty_dirs(roots: Iterable[Path], min_age_sec: float,
                     apply: bool) -> List[Path]:
    """Remove empty sweep-owned directories older than ``min_age_sec``.

    Candidates are the prefixes in :data:`_PRUNABLE_DIR_PREFIXES` that sit
    directly under ``roots``. The age gate is the point: without it this
    races a dispatch that has just created the directory and is about to
    write the file into it. Only directories are removed, never files, and
    only empty ones — a directory holding a redacted payload is the
    post-mortem copy this project deliberately keeps.
    """
    import time

    removed: List[Path] = []
    now = time.time()
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if not entry.name.startswith(_PRUNABLE_DIR_PREFIXES):
                    continue
                if now - entry.stat().st_mtime < min_age_sec:
                    continue
                if os.listdir(entry.path):
                    continue
            except OSError:
                continue
            removed.append(Path(entry.path))
            if apply:
                try:
                    os.rmdir(entry.path)
                except OSError:
                    removed.pop()
    return removed


def prune_aged_residue(roots: Iterable[Path], max_age_sec: float,
                       apply: bool) -> List[Path]:
    """Remove ``pdt-subagent-*`` directories older than ``max_age_sec``.

    The rule :func:`prune_empty_dirs` cannot express. A dispatch's
    directory holds the redacted payload kept for post-mortem reading, so
    it is not empty and never becomes empty — which is exactly why the
    empty-directory prune can never reach the population that grows by one
    per dispatch. Age is the gate that does reach it: the directory's
    mtime is when the dispatch stopped touching it, the child that wrote
    into it lives for minutes, and nothing reads the file afterwards. A
    directory older than a gate measured in months therefore cannot belong
    to a running child, whatever it holds.

    Removes the whole tree, not just the directory entry — the payload
    inside it is the thing being aged out. Candidates are restricted to
    :data:`_AGED_PRUNABLE_DIR_PREFIXES` (see that constant for why the
    lock directories are excluded) and to symlink-free direct children of
    ``roots``; a symlink is never followed and never removed.

    The age measured is *time since the directory last changed*, which is
    not always time since the dispatch. Redaction rewrites the payload
    atomically (tmpfile + ``os.replace``), and that creates and renames an
    entry in the directory — so a payload the boot sweep redacts starts
    its clock at that boot rather than at the dispatch that wrote it. That
    only ever extends the window, and never repeatedly: once a payload
    reads :data:`~utils.secret_files.REDACTED` the rewrite is a no-op and
    the mtime stops moving, so the clock does run out.
    """
    import time

    removed: List[Path] = []
    now = time.time()
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if not entry.name.startswith(_AGED_PRUNABLE_DIR_PREFIXES):
                    continue
                if now - entry.stat().st_mtime < max_age_sec:
                    continue
            except OSError:
                continue
            removed.append(Path(entry.path))
            if apply:
                try:
                    shutil.rmtree(entry.path)
                except OSError:
                    removed.pop()
    return removed


def prune_residue(roots: Iterable[Path], apply: bool) -> List[Path]:
    """Both removal rules, with their own age gates, in one call.

    The boot-time entry point: empty directories past the settle window,
    plus dispatch directories past the wide age gate. Kept as one function
    so the boot path cannot pick up one rule and silently drop the other —
    each rule is load-bearing for a different half of the population, and
    a caller that ran only the first would look correct while the count
    kept climbing.
    """
    removed = prune_empty_dirs(roots, DEFAULT_PRUNE_MIN_AGE_SEC, apply)
    removed += prune_aged_residue(roots, DEFAULT_RESIDUE_MAX_AGE_SEC, apply)
    return removed


def sweep(roots: Sequence[Path], apply: bool) -> Tuple[List[Finding], int]:
    """Classify every candidate; redact the live ones when ``apply``.

    Returns the findings and the number of redactions that *failed* —
    a managed file that still holds a credential after ``redact``
    returned False is a write error, not a success, and is reported as
    such rather than silently counted as done.
    """
    in_use = settings_paths_in_use()
    findings = [classify(p, in_use, roots) for p in find_candidates(roots)]

    if not apply:
        return findings, 0

    failures = 0
    for finding in findings:
        if not finding.needs_action:
            continue
        if redact(finding.path, flat_temp_roots=roots):
            finding.status = "redacted"
            continue
        # redact() swallows its error (correctly — losing the post-mortem
        # copy must not fail a finished task). Here a silent failure would
        # mean reporting a leak as fixed, so re-check the file.
        still_live = live_credential_keys(finding.path)
        if still_live:
            finding.status = "failed"
            finding.detail = "redact() returned False and the credential is still present"
            failures += 1
        else:
            finding.status = "redacted"
    return findings, failures


def sweep_default_roots(apply: bool = True) -> Tuple[List[Finding], int, List[Path]]:
    """Boot-time housekeeping over the machine's temp roots.

    Two halves, and both are needed to keep the population bounded:

    1. redact the payloads that still hold a credential, and
    2. remove the directories the writers leave behind.

    (2) is not optional garnish, and it is two rules rather than one. A
    dispatch writes its payload into a fresh directory, and the redaction
    keeps the *file* for post-mortem — so that directory is not empty and
    never becomes empty, which puts it permanently out of reach of an
    empty-directory prune. A dispatch that died before writing leaves the
    other kind: empty from the start. Nothing else collects either, so
    without both rules the count grows by at least one per dispatch for
    the life of the machine — see :func:`prune_residue`.

    Returns ``(findings, failures, pruned_dirs)``, where ``pruned_dirs``
    is everything either removal rule took.

    Returns immediately, having touched nothing, when :data:`DISABLE_ENV`
    is set. That is the test-isolation switch, and it gates **both** halves
    — a suite must not rewrite the developer's credentials and must not
    delete their directories either. The roots stay discoverable
    (:func:`default_roots`), and :func:`sweep` / :func:`prune_empty_dirs` /
    :func:`prune_aged_residue` are unaffected, so the pipeline is still
    exercisable against a ``tmp_path``.
    """
    if os.environ.get(DISABLE_ENV):
        return [], 0, []
    roots = default_roots()
    findings, failures = sweep(roots, apply=apply)
    pruned = prune_residue(roots, apply)
    return findings, failures, pruned
