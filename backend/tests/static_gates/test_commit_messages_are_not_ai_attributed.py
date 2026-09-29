"""Commit messages carry no AI attribution trailer (2026-09-28).

Why this gate exists
--------------------
Commits in this repository ended with a trailer naming a specific model::

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

The committer cannot know that. The *tool* is Claude Code; the model behind
any given commit is a routing decision made elsewhere and is not recorded in
the commit. Naming one is a guess written in the grammar of a fact, and it
lands in permanent public history where it cannot be corrected.

The rule is deliberately narrow, and these tests pin the narrowness as
firmly as the catch: the checker inspects the **attribution trailer**, not
the prose. A commit message may discuss Claude Code, models, providers or
AI at length — see ``test_leaves_prose_about_the_tool_alone``. Only a line
that claims authorship is rejected.

What is pinned, and where each piece lives
------------------------------------------
``scripts/check_commit_msg.py`` is the single implementation. It is reached
three ways, and a rule with three entry points needs a test on each or one
of them silently rots:

* ``.git/hooks/commit-msg`` — installed by ``scripts/install_git_hooks.sh``
  (the native path, for a clone without the pre-commit framework);
* ``.pre-commit-config.yaml`` — the ``commit-msg`` stage hook;
* ``.github/workflows/ci.yml`` — a job over the pushed range, which is what
  catches ``git commit --no-verify`` and a machine with no hooks at all.

The last test walks **this repository's own history**, so the gate is not
only about future commits: it is also the assertion that the history we
publish is clean.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHECKER = _REPO_ROOT / "scripts" / "check_commit_msg.py"
_INSTALLER = _REPO_ROOT / "scripts" / "install_git_hooks.sh"
_PRECOMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_checker():
    """Import ``scripts/check_commit_msg.py`` by path.

    ``scripts/`` is not a package and must not become one (the backend
    launches scripts with a plain interpreter), so the module is loaded
    from its file rather than imported by name.
    """
    assert _CHECKER.is_file(), (
        f"{_CHECKER} is missing — it is the single implementation of this "
        f"rule; the pre-commit hook and the CI job both call into it"
    )
    spec = importlib.util.spec_from_file_location("check_commit_msg", _CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_commit_msg", module)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _violations(message: str) -> list:
    return checker.find_violations(message)


# ---------------------------------------------------------------------------
# The catch — the shapes that make an authorship claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # The exact trailer this rule was written for. Built from fragments so
    # this test file does not itself carry the literal it rejects — the
    # same convention the other gates in this directory follow.
    "Add the phase router\n\nBody text.\n\nCo-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>\n",
    "Fix the race\n\nCo-authored-by: " + "Claude" + " <noreply@anthropic.com>\n",
    "Refactor\n\nCo-Authored-By: " + "GitHub" + " Copilot <x@example.com>\n",
    "Wire the tool\n\n\U0001f916 Generated with [" + "Claude" + " Code](https://example.invalid)\n",
    "Generated with " + "Open" + "AI Codex\n",
])
def test_flags_an_ai_attribution(message: str) -> None:
    assert _violations(message), (
        f"the checker missed an AI attribution in:\n{message!r}\n"
        f"It is the single implementation of the rule and all three entry "
        f"points call into it, so a miss here is a miss everywhere."
    )


# ---------------------------------------------------------------------------
# The narrowness — a rule that fires on prose is a rule that gets deleted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # A human co-author. The trailer shape is fine; only an AI *value* is not.
    "Pair on the parser\n\nCo-Authored-By: Dana Example <dana@example.com>\n",
    # Prose about the tool. The message is allowed to say what it was
    # produced with — what it may not do is claim a co-author.
    "Rewrite the dispatcher\n\n"
    "This was produced with Claude Code running the repo's own gates.\n"
    "The model behind the session was not recorded, so the message does\n"
    "not name one.\n",
    "Document the routing\n\nProviders and models are deployment config.\n",
    # Comments are stripped by git before the message is stored, so a
    # commented-out example in a template must not trip the hook.
    "Real subject\n\n# Co-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>\n",
    # A plain message.
    "fix(secrets): read credentials from the environment\n",
    "",
])
def test_leaves_legitimate_messages_alone(message: str) -> None:
    assert not _violations(message), (
        f"false positive on:\n{message!r}\nA checker that fires on prose "
        f"about the tool, or on a human co-author, is a checker that gets "
        f"disabled rather than fixed."
    )


# ---------------------------------------------------------------------------
# The three entry points
# ---------------------------------------------------------------------------


def test_the_pre_commit_config_wires_the_hook() -> None:
    text = _PRECOMMIT.read_text(encoding="utf-8")
    assert "commit-msg" in text, (
        ".pre-commit-config.yaml no longer declares a commit-msg stage, so "
        "the hook does not run through the pre-commit framework"
    )
    assert "scripts/check_commit_msg.py" in text, (
        ".pre-commit-config.yaml no longer calls the checker"
    )


def test_ci_wires_the_checker_over_the_pushed_range() -> None:
    text = _CI.read_text(encoding="utf-8")
    assert "commit-hygiene" in text, (
        ".github/workflows/ci.yml no longer has the commit-hygiene job; "
        "without it a --no-verify commit reaches main unchecked"
    )
    assert "scripts/check_commit_msg.py" in text, (
        "the CI job no longer calls the checker"
    )
    assert "fetch-depth: 0" in text, (
        "the commit-hygiene job must not use a shallow clone — the range it "
        "walks would not be present, and git would find nothing to check"
    )


def test_the_native_installer_covers_the_commit_msg_hook() -> None:
    """The pre-commit framework is optional; on a clone without it the
    config is inert and ``git commit`` runs nothing. The native installer is
    what makes the rule hold by default."""
    assert _INSTALLER.is_file(), (
        f"{_INSTALLER} is missing — a clone without the pre-commit "
        f"framework would then have no commit-msg hook at all"
    )
    text = _INSTALLER.read_text(encoding="utf-8")
    assert "commit-msg" in text, "the installer no longer installs a commit-msg hook"
    assert "check_commit_msg.py" in text, "the installer no longer points at the checker"


# ---------------------------------------------------------------------------
# This repository's own history
# ---------------------------------------------------------------------------


def _history_messages() -> list[tuple[str, str]]:
    """Return ``(sha, message)`` for every commit reachable from HEAD."""
    if not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no history to check")
    out = subprocess.run(
        ["git", "log", "--format=%H%x1f%B%x1e"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    ).stdout
    pairs = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\x1f")
        pairs.append((sha.strip(), body))
    return pairs


def test_no_commit_in_this_history_carries_an_ai_trailer() -> None:
    """The rule is about the published history, not only future commits.

    One ``git log`` for the whole range rather than one call per commit:
    a clone with thousands of commits would otherwise fork a process each.
    """
    history = _history_messages()
    assert history, (
        "git log returned no commits — the walk is broken, and this test "
        "would pass without checking anything"
    )

    offenders = []
    for sha, body in history:
        for v in _violations(body):
            offenders.append(f"{sha[:10]} L{v.lineno}: {v.reason}\n    {v.line}")

    assert not offenders, (
        "commits in this repository carry an AI attribution trailer. It "
        "asserts a model identity the committer could not have known, and "
        "it is in permanent public history:\n  " + "\n  ".join(offenders)
    )
