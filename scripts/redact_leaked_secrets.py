#!/usr/bin/env python3
"""Redact provider credentials left in the temp root by finished runs.

Why this script exists
----------------------
Every subagent dispatch writes a Claude Code ``--settings`` payload into a
private temp directory. That payload carries the *routed* provider's
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` — it has to, that is what
makes per-provider routing work — and it is redacted once the child
process is reaped (``utils.secret_files.redact``, wired at
``coding_tool.py``'s two shutdown points).

Redaction runs on every way a *dispatch* can end. It does **not** run when
the backend is killed, crashes, or is restarted mid-dispatch: the process
that would have cleaned up is gone. The file then keeps a live credential
for as long as it sits in the temp root — indefinitely, because nothing
else collects it.

The 0700 directory and 0600 file mode are what bound the damage in that
window — they are the layers that hold when the cleanup does not. This
script is the cleanup for the residue, so that recovering from a crash is
one command rather than a hand-edit of every file.

It is also the same sweep the backend runs on its own startup (see
``utils.secret_sweep`` and the ``_lifespan`` hook in ``server.py``). The
module holds the logic; this file is the operator's handle on it.

What it does
------------
Walks the temp root(s), finds files whose *name* matches the ones this
project's writers generate (``subagent_settings_*.json`` /
``verif_settings_*.json``, plus the nightly runner's
``nightly_ci_settings_*.json``), and for each one that still holds a live
credential, replaces it with ``<redacted>``. Everything else in the file —
``ANTHROPIC_BASE_URL``, ``ANTHROPIC_MODEL``, the hook layout — is left
intact, because that is what an operator reads when debugging a routing
mistake.

Safety properties, in the order they matter
-------------------------------------------
* **Dry run by default.** ``--apply`` is required to write anything.
* **Never touches a file a live process is using.** Paths appearing after
  ``--settings`` in the process table are skipped; redacting one out from
  under a running subagent hands it ``<redacted>`` as its API key.
* **Never follows a symlink.** A symlink named like a settings file,
  planted in a world-writable temp root, would otherwise redirect the
  rewrite at a file of the attacker's choosing.
* **Refuses anything it cannot positively identify.** Files inside a
  directory ``private_dir()`` created, or a flat-temp payload whose name
  and parent match exactly. A file that matches by name but is not ours is
  *reported*, not rewritten — the guard is load-bearing in ``coding_tool``
  and is not weakened here just to make this script's job easier.
* **Deletes directories, never files, and only under two named gates.**
  ``--prune-empty-dirs`` removes empty ``pdt-subagent-*`` /
  ``pdt-ws-locks-*`` directories older than a settle window, so it cannot
  race a dispatch that has made the directory but not yet written the
  file. ``--prune-aged-residue`` removes ``pdt-subagent-*`` directories
  older than a much wider gate whatever they hold — the same pair the
  boot-time pass runs, with the same two age gates. Both are off unless
  asked for.

Usage
-----
    python3 scripts/redact_leaked_secrets.py              # dry run
    python3 scripts/redact_leaked_secrets.py --apply      # redact
    python3 scripts/redact_leaked_secrets.py --apply --prune-empty-dirs
    python3 scripts/redact_leaked_secrets.py --prune-aged-residue  # dry run
    python3 scripts/redact_leaked_secrets.py --json       # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.secret_files import DIR_PREFIX  # noqa: E402  (needs sys.path above)
from utils.secret_sweep import (  # noqa: E402  (needs sys.path above)
    _SYMLINK,
    DEFAULT_RESIDUE_MAX_AGE_SEC,
    Finding,
    default_roots,
    prune_aged_residue,
    prune_empty_dirs,
    sweep,
)


def _print_report(findings: Sequence[Finding], failures: int,
                  removed_dirs: Sequence[Path], applied: bool) -> None:
    by_status: Dict[str, List[Finding]] = {}
    for f in findings:
        by_status.setdefault(f.status, []).append(f)

    # The headline lists the files that needed action: still ``live`` on a
    # dry run, ``redacted`` once applied. A status carried over from the
    # other mode would be a lie, so the two are read separately.
    if applied:
        headline, verb = by_status.get("redacted", []), "redacted"
    else:
        headline, verb = by_status.get("live", []), "would redact"

    if headline:
        print(f"{verb} {len(headline)} file(s) holding a credential:")
        for f in headline:
            keys = f"  — {', '.join(f.keys)}" if f.keys else ""
            print(f"  {f.path}{keys}")
    else:
        print("No unredacted credentials found.")

    for status in ("failed", _SYMLINK, "in_use", "unmanaged", "unreadable"):
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n{status}: {len(group)}")
        for f in group:
            print(f"  {f.path}" + (f"  — {f.detail}" if f.detail else ""))

    clean = by_status.get("clean", [])
    if clean:
        print(f"\nclean (already redacted or no credential): {len(clean)}")
    print(f"\ntotal candidates: {len(findings)}")

    if failures:
        print(f"\nFAILED to redact {failures} file(s) — see status 'failed' above.",
              file=sys.stderr)
    if not applied and headline:
        print("\nDry run — nothing was written. Re-run with --apply.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Redact provider credentials left in the temp root "
                    "by finished runs.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually rewrite the files (default is a dry run)",
    )
    parser.add_argument(
        "--root", action="append", default=None, metavar="DIR",
        help="scan DIR instead of the default temp roots (repeatable)",
    )
    parser.add_argument(
        "--prune-empty-dirs", action="store_true",
        help="also remove empty pdt-subagent-* directories older than "
             "--prune-min-age",
    )
    parser.add_argument(
        "--prune-min-age", type=float, default=600.0, metavar="SEC",
        help="age gate for --prune-empty-dirs (default: 600)",
    )
    parser.add_argument(
        "--prune-aged-residue", action="store_true",
        help="also remove pdt-subagent-* directories older than "
             "--residue-max-age, whether or not they are empty (the "
             "post-mortem payloads the empty-directory rule can never "
             "reach)",
    )
    parser.add_argument(
        "--residue-max-age", type=float, default=DEFAULT_RESIDUE_MAX_AGE_SEC,
        metavar="SEC",
        help=f"age gate for --prune-aged-residue "
             f"(default: {int(DEFAULT_RESIDUE_MAX_AGE_SEC)} = 90 days)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON",
    )
    parser.add_argument(
        "--fail-if-dirty", action="store_true",
        help="exit 1 when a dry run finds something to redact (for CI)",
    )
    args = parser.parse_args(argv)

    roots = [Path(r) for r in args.root] if args.root else default_roots()
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        parser.error("not a directory: " + ", ".join(str(m) for m in missing))

    findings, failures = sweep(roots, apply=args.apply)

    removed_dirs: List[Path] = []
    if args.prune_empty_dirs:
        removed_dirs = prune_empty_dirs(roots, args.prune_min_age, args.apply)
    if args.prune_aged_residue:
        removed_dirs += prune_aged_residue(
            roots, args.residue_max_age, args.apply
        )

    if args.json:
        print(json.dumps({
            "roots": [str(r) for r in roots],
            "applied": bool(args.apply),
            "findings": [f.to_dict() for f in findings],
            "failures": failures,
            "pruned_dirs": [str(d) for d in removed_dirs],
        }, indent=2, ensure_ascii=False))
    else:
        print("temp roots: " + ", ".join(str(r) for r in roots))
        _print_report(findings, failures, removed_dirs, args.apply)
        if removed_dirs:
            verb = "removed" if args.apply else "would remove"
            print(f"\n{verb} {len(removed_dirs)} residue "
                  f"director{'y' if len(removed_dirs) == 1 else 'ies'} "
                  f"({DIR_PREFIX}*, by the gates that were asked for)")

    if failures:
        return 1
    if args.fail_if_dirty and not args.apply and any(
        f.needs_action for f in findings
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
