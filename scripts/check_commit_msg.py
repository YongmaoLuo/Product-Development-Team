#!/usr/bin/env python3
"""Reject commit messages that attribute a commit to an AI model.

Why this exists
---------------
Commit messages in this repository routinely ended with a trailer like::

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

That line asserts something the committer cannot know. The *tool* is
Claude Code; the **model** behind any particular commit is a routing
decision made elsewhere, varies between sessions, and is not recorded in
the commit. Naming one specific model is therefore a guess written in the
grammar of a fact — and it is a guess that lands in permanent, public
history where it cannot be corrected.

The second reason is proportionality. A co-author trailer exists to give
credit to a *person* who shares authorship. An assistant that ran the
commands is not a co-author, and a trailer that appears on every commit
stops carrying information at all.

So the rule is narrow on purpose: **check the trailer, not the prose.**
A commit message is free to discuss Claude Code, models, providers, or AI
in general — this script only rejects the two shapes that make an
attribution claim:

  * a ``Co-Authored-By:`` trailer whose value names an AI system;
  * a ``Generated with <AI tool>`` line, with or without the 🤖.

A human co-author's trailer passes. So does a message that merely says
"wire the Claude Code CLI".

Where it runs
-------------
One implementation, three entry points, so there is no second copy of the
rule to drift:

  * ``.git/hooks/commit-msg`` — installed by ``scripts/install_git_hooks.sh``
    (and by ``pre-commit install --hook-type commit-msg`` if you use the
    pre-commit framework). Blocks the commit locally.
  * ``.pre-commit-config.yaml`` — the ``commit-msg`` stage hook.
  * ``.github/workflows/ci.yml`` — a step that checks every commit in the
    pushed range, so a commit made with ``--no-verify`` is still caught.

Usage
-----
::

    scripts/check_commit_msg.py .git/COMMIT_EDITMSG     # hook contract
    scripts/check_commit_msg.py --stdin < message.txt
    git log -1 --format=%B | scripts/check_commit_msg.py --stdin

Exit contract
-------------
  * ``0`` — no attribution trailer found
  * ``1`` — at least one violation (the message is printed with the fix)
  * ``2`` — usage / setup error (missing file, unreadable input)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# What counts as an attribution claim
# ---------------------------------------------------------------------------

#: The two line shapes that make a claim about *who wrote this*. Everything
#: else in the message is prose and is not inspected.
_TRAILER_RE = re.compile(r"^\s*co-authored-by\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE)
_GENERATED_WITH_RE = re.compile(r"\bgenerated\s+with\b", re.IGNORECASE)

#: Tokens that mark a value as naming an AI system rather than a person.
#: Matched case-insensitively as substrings, so ``claude-opus-5[1m]`` and
#: ``Claude Opus 5`` both hit.
#:
#: The product names (claude, anthropic, openai, …) are the reliable half.
#: The family names (opus, sonnet, haiku, gpt) are here because the exact
#: trailer this script exists to reject named a *model*, and a future
#: variant might name only that. They are ordinary words, so a human
#: co-author with one of them in their name would false-positive — which
#: is the right way round: a two-second rewrite beats a permanent
#: misattribution.
_AI_TOKENS: tuple[str, ...] = (
    "claude",
    "anthropic",
    "openai",
    "gemini",
    "copilot",
    "codex",
    "aider",
    "devin",
    "cursor",
    "opus",
    "sonnet",
    "haiku",
    "gpt",
)

#: Shown on failure. The point is to make the fix obvious without a
#: discussion: delete the line.
_FIX_HINT = (
    "Drop the trailer. The tool is not the author, and the model behind a "
    "given commit is not knowable from the commit — naming one writes a "
    "guess into permanent history. If you need to record how a commit was "
    "produced, put it in the commit *body* as prose, where it reads as a "
    "statement about the process rather than as a false authorship claim."
)


class Violation:
    """One offending line, with the reason it was rejected."""

    __slots__ = ("lineno", "line", "reason")

    def __init__(self, lineno: int, line: str, reason: str) -> None:
        self.lineno = lineno
        self.line = line
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Violation(lineno={self.lineno}, reason={self.reason!r})"


def _has_ai_token(value: str) -> str | None:
    """Return the token that matched, or ``None``."""
    lowered = value.lower()
    for token in _AI_TOKENS:
        if token in lowered:
            return token
    return None


def find_violations(message: str) -> list[Violation]:
    """Return every attribution claim in *message*, in line order.

    Line numbers are 1-based so the failure message can be pasted into an
    editor. Blank lines and comments (``#``) are skipped — git strips them
    from the final message anyway, and a commented-out example in a
    template should not trip the gate.
    """
    found: list[Violation] = []
    for lineno, line in enumerate(message.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        trailer = _TRAILER_RE.match(line)
        if trailer is not None:
            token = _has_ai_token(trailer.group("value"))
            if token is not None:
                found.append(
                    Violation(lineno, stripped, f"co-author trailer names an AI system ({token!r})")
                )
            continue

        if _GENERATED_WITH_RE.search(line):
            token = _has_ai_token(line)
            if token is not None:
                found.append(
                    Violation(lineno, stripped, f"'generated with' line names an AI system ({token!r})")
                )
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_message(args: argparse.Namespace) -> str:
    if args.stdin:
        return sys.stdin.read()
    path = Path(args.path)
    if not path.is_file():
        print(f"error: no such commit-message file: {path}", file=sys.stderr)
        raise SystemExit(2)
    return path.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject commit messages that attribute the commit to an AI model.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="commit-message file (git passes .git/COMMIT_EDITMSG for the commit-msg hook)",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the message from stdin instead of a file",
    )
    args = parser.parse_args(argv)

    if not args.stdin and not args.path:
        parser.error("give a commit-message file path, or --stdin")
    if args.stdin and args.path:
        parser.error("give either a path or --stdin, not both")

    violations = find_violations(_read_message(args))
    if not violations:
        return 0

    print("commit message carries an AI attribution trailer:\n", file=sys.stderr)
    for v in violations:
        print(f"  line {v.lineno}: {v.reason}", file=sys.stderr)
        print(f"    {v.line}", file=sys.stderr)
    print(f"\n{_FIX_HINT}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
