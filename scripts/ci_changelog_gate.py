#!/usr/bin/env python3
"""The changelog gate: a judgment-surface change has to carry an entry.

Why this file exists
--------------------
A verdict is flipped by a function's *return value*, not by its name.
``verification_evidence.verdict_subject_mismatch`` carries no decision
verb at all and still turns a PASSED verdict into a FAILED one — so
"which functions judge?" cannot be answered by grepping for
``_should_`` prefixes, and "did we change one of them?" cannot be
answered by grepping the changelog either.

The accident that started this work was a 137-line change to the
judgment semantics that landed inside an unrelated pull request: the
changelog said nothing about it, and the long-running process was
still holding the old module in memory, so four rounds judged by the
old rule.  The rule existed; it just was not written down where a
reader would look, and nothing made its absence loud.

What this gate decides
----------------------
Three checks, all of them mechanical (§7):

1. **Touching the judgment surface obliges an entry.**  When a change
   lands under one of :data:`PROTECTED_GLOBS`, the ``## [Unreleased]``
   block of the changelog has to have grown.
2. **The entry has to name what changed.**  It must name at least one
   of the files the change actually touched.  An entry that says "a
   judgment changed" without naming the file cannot be audited with
   ``git log`` afterwards — which is exactly how the accident above
   was dug out in the first place.
3. **The two languages have to agree.**  ``CHANGELOG.md`` and
   ``CHANGELOG.zh.md`` carry the same section and name the same files.
   A reader of one language has to learn about a judgment change the
   other language does not mention.

The judgment section itself — ``### 判定语义变更`` — is required of
*both* changelogs whether or not anything was touched; that part is
unconditional and lives in a static gate of its own.  This gate is
the conditional half: it speaks only when a judgment surface moved.

What this gate cannot decide
-----------------------------
Whether the entry is **complete**.  It reads text and a diff; it
cannot tell that the entry describes the change accurately.  §7 says
so too: *"CI 只能让「遗漏」变响，不能证明「完备」."*  Content is a
review job.

How it is run
-------------
::

    scripts/ci_changelog_gate.py --base origin/main --root .

``--root`` is the repository to inspect and ``--base`` is the ref to
compare it against.  The exit code is ``0`` when the gate is satisfied
and ``1`` when it is not; the reason is printed on one line, so a CI
log shows what the gate wanted without the reader opening the diff.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

#: The judgment surface (§9.3): a change under one of these globs
#: changes what a verdict says, and it has to be written down.  The
#: list starts at the judgment surface and stops there on purpose —
#: "not every file is protected" is a decision, and a wider list would
#: make the gate noise.
PROTECTED_GLOBS = (
    "backend/verification_*.py",
    "backend/agent.py",
    "backend/state_machine/**",
    "scripts/ci_*.sh",
    ".github/workflows/**",
)

#: The two changelogs, in the two languages they are written in.
CHANGELOGS = ("CHANGELOG.md", "CHANGELOG.zh.md")

#: The heading the material pins (§7, §12.3).  It is spelled the same
#: in both files on purpose: the gate reads one string out of both
#: changelogs, and a section that exists in only one of them is a
#: section half the readers never see.
SECTION_HEADING = "### 判定语义变更"

#: The block the entry has to live in.
UNRELEASED_HEADING = "## [Unreleased]"

#: A level-2 heading opens the next release.
_RELEASE_HEADING = re.compile(r"^## \[", re.MULTILINE)


class GateError(RuntimeError):
    """The gate could not read what it needed in order to decide."""


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a glob into a regex over repository-relative paths.

    ``*`` does not cross a ``/``; ``**`` does.  ``fnmatch`` would let a
    single ``*`` swallow a directory separator, so
    ``backend/verification_*.py`` would also match
    ``backend/sub/verification_x.py`` — a path that is not under the
    judgment surface at all.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
            index += 1
            continue
        parts.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


#: The compiled forms of :data:`PROTECTED_GLOBS`.
_PROTECTED_PATTERNS = tuple(_glob_to_regex(glob) for glob in PROTECTED_GLOBS)


def is_protected(path: str) -> bool:
    """Whether ``path`` is under the judgment surface."""
    relative = path.replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    return any(pattern.match(relative) for pattern in _PROTECTED_PATTERNS)


def _git(repo: Path, *args: str) -> str:
    """Run git inside ``repo`` and return stdout; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git_lines(repo: Path, *args: str) -> list[str]:
    """The non-empty lines git printed."""
    return [line for line in _git(repo, *args).splitlines() if line.strip()]


def _merge_base(base: str, repo: Path) -> str:
    """The commit ``base`` and ``HEAD`` have in common."""
    candidates = _git_lines(repo, "merge-base", base, "HEAD")
    if not candidates:
        raise GateError(f"no merge base between {base} and HEAD")
    return candidates[0]


def changed_paths(base: str, repo: Path) -> list[str]:
    """Every path that differs between ``base`` and the working tree.

    ``base`` is resolved to its merge base with ``HEAD`` first: a branch
    that has fallen behind its base must not report the base's own
    commits as its changes.  The working tree is included, so the gate
    also works as a pre-commit check, and untracked files are listed
    too — a *new* judgment module that has never been committed is
    still a judgment module.
    """
    merge_base = _merge_base(base, repo)
    tracked = _git_lines(repo, "diff", "--name-only", merge_base)
    untracked = _git_lines(repo, "ls-files", "--others", "--exclude-standard")
    return sorted(set(tracked) | set(untracked))


def _unreleased_block(text: str) -> str:
    """The text between ``## [Unreleased]`` and the next release heading.

    Empty when the file carries no ``## [Unreleased]`` block at all.
    """
    start = text.find(UNRELEASED_HEADING)
    if start == -1:
        return ""
    body = start + len(UNRELEASED_HEADING)
    match = _RELEASE_HEADING.search(text, body)
    end = match.start() if match else len(text)
    return text[body:end]


def _new_lines(before: str, after: str) -> list[str]:
    """The lines ``after`` has that ``before`` did not.

    A multiset difference rather than a set difference, so that a
    second identical bullet still counts as a new line — and so that
    *removing* a line does not look like adding one.
    """
    before_counts = Counter(before.splitlines())
    after_counts = Counter(after.splitlines())
    extra: list[str] = []
    for line, count in after_counts.items():
        extra.extend([line] * max(0, count - before_counts.get(line, 0)))
    return extra


def _mentions(entry: str, path: str) -> bool:
    """Whether ``entry`` names the file ``path``.

    The whole repository-relative path counts.  The bare file name
    counts too — a changelog that says ``verification_evidence.py`` has
    named the file — but only on a word boundary, so that
    ``verification_evidence_helpers.py`` does not pass for
    ``verification_evidence.py``.
    """
    if path in entry:
        return True
    name = path.rsplit("/", 1)[-1]
    if not name:
        return False
    return re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", entry) is not None


def named_files(entry: str, changed: Iterable[str]) -> set[str]:
    """The changed files the entry names."""
    return {path for path in changed if _mentions(entry, path)}


def check_entry(text: str, changed: Sequence[str]) -> list[str]:
    """The problems with one language's changelog entry.

    ``text`` is the whole changelog file; only its ``## [Unreleased]``
    block is judged.  ``changed`` are the protected paths the change
    touched.

    Two things are asked of the entry:

    1. it carries the ``### 判定语义变更`` section — a section that
       exists in only one of the two languages is a section half the
       readers never see;
    2. it names at least one of the files the change actually touched
       — an entry that says "a judgment changed" without naming the
       file cannot be audited afterwards.

    The returned list is empty when the entry is fine.
    """
    entry = _unreleased_block(text)
    problems: list[str] = []
    if SECTION_HEADING not in entry:
        problems.append(f"缺少 {SECTION_HEADING} 小节")
    if changed and not named_files(entry, changed):
        problems.append("条目未点名实际改动的判定文件: " + ", ".join(changed))
    return problems


def _read_text(path: Path) -> str:
    """The file's text; empty when the file is not there."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return ""


def _git_show(repo: Path, ref: str, path: str) -> str:
    """The file ``path`` as it was at ``ref``; empty when it was not there."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    """Decide whether the change carries its changelog entry.

    Returns ``0`` when the gate is satisfied and ``1`` when it is not.
    """
    parser = argparse.ArgumentParser(
        description="Fail when a judgment-surface change carries no changelog entry.",
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="the ref the change is compared against",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="the repository to inspect",
    )
    args = parser.parse_args(argv)
    repo = Path(args.root).resolve()

    try:
        changed = changed_paths(args.base, repo)
        protected = [path for path in changed if is_protected(path)]
        if not protected:
            print("CHANGELOG GATE: PASS — 未触及受保护路径")
            return 0

        merge_base = _merge_base(args.base, repo)
        current: dict[str, str] = {}
        before: dict[str, str] = {}
        for name in CHANGELOGS:
            current[name] = _read_text(repo / name)
            before[name] = _git_show(repo, merge_base, name)
    except GateError as error:
        print(f"CHANGELOG GATE: FAIL — {error}")
        return 1

    # Check 1 — the entry has to be new.  This is the check the other
    # two depend on: without new lines there is no entry to judge, and
    # saying so is more useful than saying three things at once.
    added = 0
    for name in CHANGELOGS:
        added += len(
            _new_lines(
                _unreleased_block(before[name]),
                _unreleased_block(current[name]),
            )
        )
    if added == 0:
        print("CHANGELOG GATE: FAIL — 判定面改动缺少 [Unreleased] 条目")
        return 1

    problems: list[str] = []
    for name in CHANGELOGS:
        problems.extend(f"{name}: {problem}" for problem in check_entry(current[name], protected))

    # Check 3 — the two languages have to say the same thing.
    named = [
        named_files(_unreleased_block(current[name]), protected) for name in CHANGELOGS
    ]
    if named[0] != named[1]:
        problems.append("中英不同步")

    if problems:
        print("CHANGELOG GATE: FAIL — " + "; ".join(problems))
        return 1

    print("CHANGELOG GATE: PASS — 判定面改动已带 [Unreleased] 条目")
    return 0


if __name__ == "__main__":
    sys.exit(main())
