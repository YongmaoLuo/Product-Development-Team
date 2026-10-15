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

The privacy rule, added 2026-10-14
----------------------------------
This is also the entry point for the second commit-msg rule: a plan-id
shape anywhere in the message. That is the rule the 2026-09-29 incident
would have needed, and it is here rather than in the tree gates because
this hook runs *before the commit exists* — the only point at which
refusing costs nothing.

The shape is not restated here. ``find_plan_ids_in_metadata`` is imported
from ``backend/tests/static_gates/test_no_plan_ids_in_metadata.py``, the
same function the commit-range scan and the pull-request metadata scan use,
so the four surfaces cannot drift apart.

Why the import does not pull in pytest: the hook is invoked as
``python3 scripts/check_commit_msg.py`` — a bare interpreter, no venv — so
this script adds the gate directory to ``sys.path`` and relies on that
module being stdlib-only.

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

# One definition of "a plan id", shared with the tree gate, the commit-range
# scan and the pull-request metadata scan. Imported rather than restated:
# this file runs under a bare ``python3`` from the commit-msg hook, with no
# venv and no third-party packages, which is why the module it reaches for
# is stdlib-only (no ``import pytest``).
_GATES_DIR = Path(__file__).resolve().parent.parent / "backend/tests/static_gates"
if not _GATES_DIR.is_dir():  # pragma: no cover - broken checkout
    print(f"error: static gates directory not found: {_GATES_DIR}", file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(_GATES_DIR))

try:
    # The same ``redact_span`` every other gate renders findings with, and
    # for the same reason. Imported from ``source_scan`` rather than
    # restated because the *shape* of a redaction has to agree across the
    # tree: a reader who has seen one report has seen the rest.
    from source_scan import redact_span  # noqa: E402
    from test_no_plan_ids_in_metadata import (  # noqa: E402
        COMMIT_MESSAGE_LABEL,
        find_plan_ids_in_metadata,
    )
except ImportError as exc:  # pragma: no cover - broken checkout
    print(f"error: cannot import the plan-id rule from {_GATES_DIR}: {exc}", file=sys.stderr)
    raise SystemExit(2)

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
#:
#: ``agent``, ``assistant`` and ``bot`` were added 2026-10-14, and they
#: are the reason the rule can be talked about as covering "an AI system"
#: rather than "one of these product names". The trailer that got through
#: read ``Co-Authored-By: Autonomous Agent <agent@…>``: no vendor name, no
#: model family, nothing any of the tokens above match — and a gate that
#: only catches leaks spelled the way the last leak was spelled is a gate
#: waiting for the next spelling. Every vendor now ships something whose
#: name is one of those three words, so they are the high-value half.
#:
#: Deliberately *not* here, though it looks like it belongs: ``ai``. These
#: are substring matches, and ``ai`` is inside ``email``, ``maintain``,
#: ``chain``, ``detail`` and most of the English a commit message is made
#: of. Adding it would not narrow the rule, it would delete it. A token
#: has to be specific enough that the word cannot occur by accident.
#:
#: ``bot`` and ``agent`` are ordinary words in their own right, and this
#: is a real cost: a human co-author called Abbot, or a team called
#: "Support Agents", trips the gate. That trade is deliberate and is
#: pinned by a test rather than left hypothetical — see
#: ``test_commit_messages_are_not_ai_attributed.py``. It is the same
#: trade ``opus`` and ``gpt`` already make, and it is the cheaper
#: direction: the failure mode
#: of a false positive is a rewritten co-author line, and the failure mode
#: of a false negative is permanent, public, uncorrectable history.
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
    # Product names that are also ordinary English. See above.
    "agent",
    "assistant",
    "bot",
    # Coding agents that are neither a vendor nor a model family.
    "codeium",
    "windsurf",
    "tabnine",
    "opencode",
    # Model families absent from the list above.
    "llama",
    "mistral",
    "deepseek",
    "qwen",
    "grok",
)

#: Shown when a plan id is found. A commit message is the one surface where
#: there is no excuse: nothing under ``tests/`` is reachable from one, so
#: every date-prefixed id here was typed by hand. There is no placeholder
#: exemption — the repository's own neutral date placeholder matches the shape
#: too, and allowing it would make "no false positives by construction"
#: untrue. A placeholder in a commit message is still a quoted identifier.
_PLAN_ID_FIX_HINT = (
    "Rewrite the id, don't quote it. A plan id in a commit message lands in "
    "permanent public history through GitHub's merge commit, where a "
    "force-push cannot remove it — deleting a ref does not delete objects. "
    "Describe the work instead: the plan's date and name are private, the "
    "behaviour change is not."
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


#: The two rules this module enforces, as stable short names. They are
#: part of :attr:`Violation.rule` rather than something a caller recovers
#: from :attr:`Violation.reason`, because a caller that buckets findings by
#: a substring of the human-readable reason is one wording change away from
#: silently merging two rules into one report. These names are not private:
#: they are what a consumer prints to say which rule a finding came from.
RULE_TRAILER = "trailer"
RULE_PLAN_ID = "plan-id"


class Violation:
    """One offending line, with the rule it broke and why.

    *rule* is one of :data:`RULE_TRAILER` / :data:`RULE_PLAN_ID`. The two
    are independent: a message can break both, and a caller that needs to
    report on one of them must be able to tell which findings it is
    looking at. *reason* is for a human and is not a stable key.
    """

    __slots__ = ("lineno", "line", "reason", "rule")

    def __init__(self, lineno: int, line: str, reason: str, rule: str) -> None:
        self.lineno = lineno
        self.line = line
        self.reason = reason
        self.rule = rule

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Violation(lineno={self.lineno}, rule={self.rule!r}, reason={self.reason!r})"


def _has_ai_token(value: str) -> str | None:
    """Return the token that matched, or ``None``."""
    lowered = value.lower()
    for token in _AI_TOKENS:
        if token in lowered:
            return token
    return None


def find_violations(message: str) -> list[Violation]:
    """Return every finding against *message*, in line order.

    This enforces **two** independent rules, and each finding says which
    one it came from in :attr:`Violation.rule`: :data:`RULE_TRAILER` (a
    ``Co-Authored-By:`` or "Generated with" line naming an AI system) and
    :data:`RULE_PLAN_ID` (a date-prefixed plan id anywhere in the message).
    The two have different remedies and different exemptions, so a caller
    that reports on them as one thing reports the wrong fix for half of
    what it found.

    Line numbers are 1-based so the failure message can be pasted into an
    editor. Blank lines and comments (``#``) are skipped — git strips them
    from the final message anyway, and a commented-out example in a
    template should not trip the gate.

    Unlike the trailer rule, the plan-id rule reads **every** line: git's
    comment-stripping only applies to the template scaffolding, and a
    quoted plan id in the body is exactly as permanent as one in the
    subject.
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
                    Violation(
                        lineno,
                        stripped,
                        "co-author trailer names an AI system: "
                        f"{redact_span(token)}",
                        RULE_TRAILER,
                    )
                )
            continue

        if _GENERATED_WITH_RE.search(line):
            token = _has_ai_token(line)
            if token is not None:
                found.append(
                    Violation(
                        lineno,
                        stripped,
                        "'generated with' line names an AI system: "
                        f"{redact_span(token)}",
                        RULE_TRAILER,
                    )
                )

    for _, lineno, plan_id in find_plan_ids_in_metadata(COMMIT_MESSAGE_LABEL, message):
        found.append(
            Violation(
                lineno,
                _line_at(message, lineno).strip(),
                f"commit message carries a plan id: {redact_span(plan_id)}",
                RULE_PLAN_ID,
            )
        )
    return found


def _line_at(message: str, lineno: int) -> str:
    """Return the 1-based *lineno* line of *message*, or ``""``.

    The shared rule reports a line number so the message can be pasted into
    an editor; the line text is re-read here so the failure output quotes it
    rather than the id alone.
    """
    lines = message.splitlines()
    return lines[lineno - 1] if 1 <= lineno <= len(lines) else ""


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

    attribution = [v for v in violations if v.rule == RULE_TRAILER]
    plan_ids = [v for v in violations if v.rule == RULE_PLAN_ID]

    # ``v.line`` is the offending commit-message line and is deliberately
    # NOT printed. This is a commit-msg hook: its stderr lands in whatever
    # the committer is looking at, and the message it is rejecting is by
    # construction a message that quotes a private plan name. The reason
    # already names the rule, and the line number already says where.
    if attribution:
        print("commit message carries an AI attribution trailer:\n", file=sys.stderr)
        for v in attribution:
            print(f"  line {v.lineno}: {v.reason}", file=sys.stderr)
        print(f"\n{_FIX_HINT}", file=sys.stderr)

    if plan_ids:
        print("commit message carries a plan id:\n", file=sys.stderr)
        for v in plan_ids:
            print(f"  line {v.lineno}: {v.reason}", file=sys.stderr)
        print(f"\n{_PLAN_ID_FIX_HINT}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
