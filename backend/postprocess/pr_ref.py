"""The pull request a command is about.

What this module is
-------------------
A command that asks a human to approve something is usually about one
pull request: ``gh pr merge 42 --repo owner/name --squash``.  The
waiting row that asks the human has to say *which* pull request it is
about — the number, and the link a human can click (architecture
decision point R5) — and a reader of the waiting rows has to be able
to tell an approval apart from an ordinary question put to a human
(decision point R6).

``gh`` prints the link itself, but a command that is only *about* to
run has printed nothing yet.  So the link is composed here from the
two facts the command carries: the number and the repository.

What this module is NOT
-----------------------
It does not run ``gh`` and it does not ask GitHub anything.  It reads
an argv that is already in hand — the argv the executor refused to
run — and says what that argv is about.  A command it cannot read is
a command it says nothing about: the wait is still created, it simply
carries no pull request.

The edges it refuses
--------------------
* **No number** — ``gh pr merge --continue`` names no pull request.
  The result is ``None``: there is nothing to point at.
* **A number that is not one** — ``gh pr merge soon`` is not a pull
  request number, and guessing would point a human at somebody else's
  pull request.  The result is ``None`` as well.
* **A number without a repository** — ``gh pr merge 42`` names pull
  request 42 of *this* repository, which the caller knows and this
  module does not.  The number is kept and the link is left empty: a
  half-known pull request is better than a wrong one.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple


#: The link of a pull request on GitHub.
GITHUB_PULL_URL_TEMPLATE = "https://github.com/{owner}/{name}/pull/{number}"

#: The ``gh`` subcommands that talk about **one** pull request.  A
#: command under any other subcommand names no pull request this
#: module is willing to read.
PR_SUBCOMMANDS: Tuple[str, ...] = ("merge", "view")

#: The names the ``gh`` binary goes by — the argv may carry a full
#: path to it.
_GH_NAMES: Tuple[str, ...] = ("gh", "gh.exe")

#: The flags of the two subcommands above that carry a **value**.  The
#: value of such a flag is not the pull request number, whatever it
#: looks like: ``--repo owner/name`` is a repository, not a number.
_VALUE_FLAGS: Tuple[str, ...] = (
    "--repo",
    "-R",
    "--body",
    "-b",
    "--body-file",
    "-F",
    "--match-head-commit",
    "--author-email",
    "--json",
    "--jq",
    "--template",
    "-t",
)

#: A pull request number — decimal digits and nothing else.
_DECIMAL = re.compile(r"^[0-9]+$")


def extract_pr_ref(argv: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """The pull request a command names, if it names one.

    Args:
        argv: The command, as the list of arguments it would be run
            with.  Every part is read as text.

    Returns:
        ``{"pr_number": int, "pr_url": str | None}`` when the command
        names a pull request, and ``None`` when it does not.  The link
        is ``None`` when the command carries no repository: the number
        is still kept, because the number is what the command really
        says.
    """
    parts = [str(part) for part in (argv or [])]
    index = _pr_index(parts)
    if index is None:
        return None

    rest = parts[index + 2:]
    number = _number_of(rest)
    if number is None:
        return None

    repo = _repo_of(rest)
    url = None
    if repo is not None:
        owner, name = repo
        url = GITHUB_PULL_URL_TEMPLATE.format(owner=owner, name=name, number=number)

    return {"pr_number": number, "pr_url": url}


def _pr_index(parts: Sequence[str]) -> Optional[int]:
    """Where the ``pr`` of a ``gh pr ...`` command sits.

    The ``pr`` is only read when it follows the ``gh`` binary itself:
    a word called ``pr`` inside some other command is not a pull
    request command.
    """
    for index in range(1, len(parts)):
        if parts[index] != "pr":
            continue
        if _is_gh(parts[index - 1]):
            if index + 1 < len(parts) and parts[index + 1] in PR_SUBCOMMANDS:
                return index
    return None


def _is_gh(token: str) -> bool:
    """Whether a token names the ``gh`` binary."""
    return token.replace("\\", "/").rsplit("/", 1)[-1] in _GH_NAMES


def _number_of(tokens: Sequence[str]) -> Optional[int]:
    """The pull request number a list of arguments names.

    The first argument that is not a flag and not the value of a flag
    is the number — and it has to be one: an argument that is not a
    number means the command names no pull request this module is
    willing to read.
    """
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-") and token != "-":
            # A flag that carries a value takes the next argument with
            # it; a flag that does not takes nothing.
            if token in _VALUE_FLAGS:
                skip_next = True
            continue
        if not _DECIMAL.match(token):
            return None
        return int(token)
    return None


def _repo_of(tokens: Sequence[str]) -> Optional[Tuple[str, str]]:
    """The ``owner/name`` a list of arguments names, if it names one."""
    for index, token in enumerate(tokens):
        if token in ("--repo", "-R"):
            if index + 1 < len(tokens):
                return _split_repo(tokens[index + 1])
            return None
        if token.startswith("--repo="):
            return _split_repo(token.split("=", 1)[1])
    return None


def _split_repo(value: str) -> Optional[Tuple[str, str]]:
    """An ``owner/name`` pair, or nothing when the value is not one."""
    parts: List[str] = value.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]
