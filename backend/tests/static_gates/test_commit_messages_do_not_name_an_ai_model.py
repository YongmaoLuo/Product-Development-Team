"""Commit messages do not name an AI model, and carry no plan id.

What this gate is
-----------------
A commit message may say **what changed and why**, and it may name the
**tool** that produced the work. It may not name the **model** behind it,
and it may not carry a **plan id** — the private, dated name a plan is
known by inside this project.

Two rules, two different reasons
--------------------------------
**The model name.** The tool is a program somebody ran; the model behind a
given commit is a routing decision made elsewhere, varies between
sessions, and is not recorded in the commit. So a trailer may say
``Co-Authored-By: Claude Code`` — that is a fact about the work — and may
not say ``Co-Authored-By: Claude Opus 5``, which is a guess written in
the grammar of a fact and landed in permanent, public history where it
cannot be corrected.

The line between the two is drawn by **removing the tool phrases first
and then looking for a model name in what is left**. That ordering is the
rule, not an implementation detail: a tool name can contain a
model-family word — ``Gemini CLI`` contains ``gemini`` — and naming the
tool must not trip the model check. A value naming both, ``Claude Code
(Opus 5)``, is still refused, because after the tool phrase has been
taken out the model name is still there.

**The plan id.** The shape is ``20[0-9]{6}-<slug>``, and it is the same
shape the tree gates, the commit-range scan and the pull-request
metadata scan all use, so the four surfaces cannot drift apart. Nothing
under ``tests/`` is reachable from a commit message, so a date-prefixed
id in one was typed by hand, every time, on purpose. The rule is
therefore a straight application of the shape with **zero exemptions**,
and the commit-msg hook is the cheapest place to enforce it: it runs
*before the commit exists*, which is the only point at which refusing
costs nothing.

What is pinned, and where each piece lives
------------------------------------------
``scripts/check_commit_msg.py`` is the single implementation. It is
reached three ways, and a rule with three entry points needs a test on
each or one of them silently rots:

* ``.git/hooks/commit-msg`` — installed by ``scripts/install_git_hooks.sh``
  (the native path, for a clone without the pre-commit framework);
* ``.pre-commit-config.yaml`` — the ``commit-msg`` stage hook;
* ``.github/workflows/ci.yml`` — a job over the pushed range, which is what
  catches ``git commit --no-verify`` and a machine with no hooks at all.

The tests below are, in order:

* the **surface**: the checker publishes exactly two rules, and they are
  the model-name rule and the plan-id rule.
* the **catch** and its **narrowness**, for each rule: a message that
  names a model is refused, a message that names only a tool is left
  alone, and a message carrying a plan id is refused.
* the **boundary** the model scan is drawn on, and the **false positive**
  it deliberately costs.
* the **CLI contract**: ``0`` / ``1`` / ``2``, and ``--stdin``.
* the **three entry points**.
* **this repository's own history** — the rule is not only about future
  commits: it is also the assertion that the history we publish is clean.
* the **ref sweep** over the refs that can still be published, and the
  **push rule** that reads the remote-tracking refs.
* three **negative controls**, so that "no findings" can never be
  satisfied by a scan that looked at nothing: a planted plan id in a
  throwaway repository, a planted plan id on a published ref, and an empty
  repository whose surface must walk nothing.

Refs other than HEAD
--------------------
The sweep reads the refs that can still become public — ``HEAD`` (whichever
branch is checked out, and about to be pushed or merged whatever it is
called), ``refs/remotes/**`` and ``refs/tags/**``. What is deliberately
**not** in it is ``refs/heads/**``: sweeping it meant sweeping every
development branch, and a branch that is never pushed cannot leak. A local
branch is still checked twice over — ``HEAD`` is in the surface whichever
branch is checked out, and anything reaching a remote has to pass the
pre-push hook on the way, which is the one gate that runs *before*
publication rather than after it.

Narrowing a scan surface has one failure mode that no assertion downstream
can catch: the surface becomes empty, or stops containing anything
reachable, and the tests that read it go green because they stopped
looking. That is what the negative control on the narrowed surface is
for, and the counts the walk produced itself are asserted non-zero before
any verdict is read.

What a green run of this file means
-----------------------------------
It means the model-name rule and the plan-id rule are enforced on this
tree and on the history this tree publishes. It does not mean the rules
are the only things a commit message could ever say wrongly, and it does
not mean the history of this repository is free of anything else — it
means *these* rules hold. The counts a reader gets from re-running the
command are the ones that can be reproduced; a number quoted here belongs
to the tree that produced it and moves with it.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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

#: The rules a finding can break. Read off the checker rather than
#: spelled out here so the two cannot drift: this name is what the
#: failure output prints to say which rule sent a ref red, and a copy
#: that drifts is a report that sorts under a heading nothing was ever
#: checked against.
_RULE_TRAILER = checker.RULE_TRAILER
_RULE_PLAN_ID = checker.RULE_PLAN_ID

#: The same two rules as *values* — what a finding's ``rule`` attribute
#: actually holds.
_RULES: frozenset[str] = frozenset({_RULE_TRAILER, _RULE_PLAN_ID})

#: The names the checker publishes, as a set. Read off the module rather
#: than spelled out, so a rule that appears or disappears shows up here
#: instead of in a hand-written list that could be edited to match.
_RULE_NAMES: frozenset[str] = frozenset({
    n for n in dir(checker) if n.startswith("RULE_")
})

#: A plan id that matches :data:`PLAN_ID_SHAPE`, assembled from fragments so
#: this file does not carry the literal it plants. Same convention as
#: ``_SYNTHETIC_PLAN_ID`` in ``test_no_plan_ids_in_metadata``.
_PLAN_ID = "2026" + "1012-example" + "-topic"


def _violations(message: str) -> list:
    return checker.find_violations(message)


# ---------------------------------------------------------------------------
# The surface — the script does two things
# ---------------------------------------------------------------------------


def test_the_checker_enforces_exactly_two_rules() -> None:
    """The checker publishes two rules: the model name and the plan id.

    The rule set is read off the module rather than inferred from a
    behaviour. A rule appearing or disappearing is the signal, and this
    is the test that goes red when one does: a script that silently grew
    a third rule, or dropped one, would look exactly like this one from
    every entry point.
    """
    assert _RULE_NAMES == frozenset({"RULE_PLAN_ID", "RULE_TRAILER"}), (
        f"the checker publishes {sorted(_RULE_NAMES)}; it is supposed to "
        f"publish exactly two rules, the model-name rule and the plan-id "
        f"rule. A rule that came back or went away is not something this "
        f"file can see from the outside, so it is read off the module."
    )
    assert _RULES == frozenset({"trailer", "plan-id"}), (
        f"the rule values are {sorted(_RULES)}; a finding is bucketed by "
        f"its rule, and a bucket that does not match the rule it broke "
        f"reports the wrong remedy for it."
    )


# ---------------------------------------------------------------------------
# The catch: a model name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # The trailer this rule exists for, in the shape it actually had.
    "Co-Authored-By: Claude Opus 5\n",
    "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n",
    "Co-Authored-By: Claude Sonnet 4.5\n",
    "Co-Authored-By: Claude Haiku\n",
    "Co-Authored-By: GPT-5\n",
    "Co-Authored-By: DeepSeek V3\n",
    # The other line shape the rule covers.
    "Generated with Claude Opus 5\n",
    "Generated with 🤖 Claude Opus 5\n",
    # Tool *and* model: the tool phrase comes off, and the model name is
    # still there. This is the case that says the rule is about the model
    # and not about the presence of a vendor word.
    "Co-Authored-By: Claude Code (Opus 5)\n",
    "Generated with Claude Code (Opus 5)\n",
    # The same, for a tool whose name *contains* a model-family word: the
    # tool phrase is removed whole, and what is left still names a model.
    "Co-Authored-By: Gemini CLI (Gemini 2.5)\n",
    "Co-Authored-By: Gemini 2.5 Pro\n",
])
def test_flags_an_ai_model_name(message: str) -> None:
    findings = _violations(message)
    assert findings, (
        f"the checker missed an AI model name in:\n{message!r}\n"
        f"It is the single implementation of the rule and all three entry "
        f"points call into it, so a miss here is a miss everywhere."
    )
    assert {f.rule for f in findings} == {_RULE_TRAILER}, (
        f"the finding is filed under "
        f"{sorted({f.rule for f in findings})}, not {_RULE_TRAILER!r}. The "
        f"bucket a finding is filed under has to be the rule it broke, or "
        f"the remedy printed next to it belongs to a different rule."
    )
    assert any("names an AI model" in f.reason for f in findings), (
        f"the finding does not say which rule broke: "
        f"{[f.reason for f in findings]!r}"
    )


# ---------------------------------------------------------------------------
# The narrowness: a tool name, and a human
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # The tool, with the vendor's own contact shape.
    "Co-Authored-By: Claude Code <noreply@anthropic.com>\n",
    "Co-Authored-By: Cursor\n",
    "Co-Authored-By: GitHub Copilot\n",
    "Co-Authored-By: GitHub Copilot <copilot@github.com>\n",
    "Co-Authored-By: Gemini CLI <noreply@google.com>\n",
    "Co-Authored-By: Windsurf <w@example.com>\n",
    "Co-Authored-By: Aider <a@example.com>\n",
    "Co-Authored-By: CodeX <cx@example.com>\n",
    # The other line shape, naming the tool.
    "Generated with Claude Code\n",
    # A human co-author, who is the case the trailer was invented for.
    "Co-Authored-By: Jane Doe <jane@example.com>\n",
])
def test_leaves_tool_names_alone(message: str) -> None:
    assert not _violations(message), (
        f"false positive on:\n{message!r}\n"
        f"The rule is about model names. A trailer that names the tool is "
        f"a fact about the work, and a human co-author is the case the "
        f"trailer was invented for — neither may be refused."
    )


def test_the_model_scan_matches_on_word_boundaries_only() -> None:
    """A model token is a *word*, not a letter sequence.

    The model names are matched with word boundaries, so a name that
    merely contains one — ``Opusio``, ``Sonnetina`` — is not a model
    name, and a token glued to a hyphen still is: ``GPT-5`` is a hit
    because a hyphen is not a word character.
    """
    for value in (
        "Opusio <opusio@example.com>",
        "Sonnetina <sonnetina@example.com>",
        "Xgpt <xgpt@example.com>",
    ):
        message = f"Co-Authored-By: {value}\n"
        assert not _violations(message), (
            f"false positive on:\n{message!r}\n"
            f"The model names are matched on word boundaries, so a token "
            f"inside a longer word is not a hit. Matching on substrings "
            f"would make the rule a coin flip on the co-author's "
            f"vocabulary rather than on what the trailer names."
        )

    assert _violations("Co-Authored-By: GPT-5\n"), (
        "a hyphen is not a word character, so 'GPT-5' names a model and "
        "has to be caught"
    )


def test_the_false_positive_the_model_rule_costs_is_pinned() -> None:
    """Some model names are ordinary words. That cost is deliberate.

    ``sonnet`` is a poem, ``mistral`` is a wind, ``ernie`` is a name. A
    human co-author carrying one of them in their name trips the gate.
    That trade is deliberate and is pinned by a test rather than left
    hypothetical: the two ways this rule can be wrong are not symmetric —
    a false positive costs a rewritten co-author line, and a false
    negative is permanent public history that cannot be corrected.
    Writing the cost down as a test means the next person to widen the
    token list sees what widening already costs.
    """
    for value in (
        "Mistral Example <mistral@example.com>",
        "Ernie Example <ernie@example.com>",
    ):
        human = f"Ship the parser\n\nCo-Authored-By: {value}\n"
        assert _violations(human), (
            f"if this now passes, the model-name list was narrowed — which "
            f"is fine and is a change to make deliberately, with the "
            f"false-positive cost written down rather than rediscovered"
        )


# ---------------------------------------------------------------------------
# The catch: a plan id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # The id in the subject.
    "see " + _PLAN_ID + " for the rewrite\n",
    # The id in the body, on its own line.
    "Refactor\n\nThe work is described by " + _PLAN_ID + ".\n",
    # The id glued to a longer slug. The shape is greedy on purpose: a
    # slug glued to the id is part of the id, not the place where the id
    # stops, because ``fix/<id>-zz-retro-widgets`` is what actually
    # reaches the merge commit.
    "fix/" + _PLAN_ID + "-zz-retro-widgets\n",
    # The id inside inline code. Backticks are not a quotation mark the
    # rule knows about, and a quoted identifier is still an identifier.
    "See `" + _PLAN_ID + "` in the tracker\n",
    # The id on the last line, with no trailing newline.
    "Refactor\n\nThe work is described by " + _PLAN_ID,
])
def test_flags_a_plan_id(message: str) -> None:
    findings = _violations(message)
    assert findings, (
        f"the checker missed a plan id in:\n{message!r}\n"
        f"It is the single implementation of the rule and all three entry "
        f"points call into it, so a miss here is a miss everywhere."
    )
    assert {f.rule for f in findings} == {_RULE_PLAN_ID}, (
        f"the finding is filed under "
        f"{sorted({f.rule for f in findings})}, not {_RULE_PLAN_ID!r}. The "
        f"bucket a finding is filed under has to be the rule it broke, or "
        f"the remedy printed next to it belongs to a different rule."
    )


@pytest.mark.parametrize("message", [
    # This repository's real subjects, unchanged.
    "fix(verification): the revocation has to actually take effect\n",
    "refactor(env): drop the three fake required fields\n",
    "test(suite): split the implicit ordering coupling out of the unit suite\n",
    "Merge pull request #40 from YongmaoLuo/refactor/one-dotenv-at-the-root\n",
    # A date that is not a plan id: the shape needs eight digits and a
    # hyphen, so a bare year or a build number is not one.
    "bump the build to 2026\n",
    # A number with the digits but no slug after the hyphen.
    "the incident happened on 20261012\n",
    # A plain message, and an empty one.
    "fix(secrets): read credentials from the environment\n",
    "",
])
def test_leaves_ordinary_messages_alone(message: str) -> None:
    assert not _violations(message), (
        f"false positive on:\n{message!r}\nThe rule is a straight "
        f"application of the plan-id shape with no exemptions; a message "
        f"that does not carry the shape has to pass."
    )


# ---------------------------------------------------------------------------
# The CLI contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message, expected_rc, expected_rule", [
    # The model name: refused, under the model rule.
    ("Co-Authored-By: Claude Opus 5\n", 1, _RULE_TRAILER),
    # The tool alone: allowed.
    ("Co-Authored-By: Claude Code\n", 0, None),
    # The tool and the model: refused, under the model rule.
    ("Co-Authored-By: Claude Code (Opus 5)\n", 1, _RULE_TRAILER),
    # The plan id: refused, under the plan-id rule.
    ("see " + _PLAN_ID + " for the rewrite\n", 1, _RULE_PLAN_ID),
])
def test_the_cli_exit_code_follows_the_rule(message, expected_rc, expected_rule) -> None:
    """The exit status, and the rule it came from, for the four shapes.

    Three entry points call this script as a subprocess and read nothing
    but its exit status: the git hook, the CI job's per-commit loop, and
    the pre-commit framework's ``commit-msg`` stage. The status is the
    whole interface, so it is pinned here rather than left to the callers
    to discover.
    """
    done = subprocess.run(
        [sys.executable, str(_CHECKER), "--stdin"],
        input=message, capture_output=True, text=True, check=False,
    )
    assert done.returncode == expected_rc, (
        f"the checker returned {done.returncode} for {message!r}, expected "
        f"{expected_rc}; {done.stderr!r}"
    )
    rules = {v.rule for v in _violations(message)}
    if expected_rule is None:
        assert not rules, (
            f"the checker returned {done.returncode} but still reported "
            f"{sorted(rules)} for {message!r}"
        )
    else:
        assert rules == {expected_rule}, (
            f"the finding for {message!r} is filed under {sorted(rules)}, "
            f"not {expected_rule!r}"
        )


def test_the_cli_exit_contract_is_preserved() -> None:
    """``0`` / ``1`` / ``2``, and ``--stdin``."""
    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_CHECKER), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    clean = run("--stdin", stdin="fix(secrets): read credentials from the environment\n")
    assert clean.returncode == 0, (
        f"a message with no violation returned {clean.returncode}; "
        f"{clean.stderr!r}"
    )

    dirty = run("--stdin", stdin="see " + _PLAN_ID + " for the rewrite\n")
    assert dirty.returncode == 1, (
        f"a message carrying a plan id returned {dirty.returncode}, not 1; "
        f"{dirty.stderr!r}"
    )
    assert "plan id" in dirty.stderr, (
        f"the failure output does not say what the rule is: {dirty.stderr!r}"
    )

    named = run("--stdin", stdin="Co-Authored-By: Claude Opus 5\n")
    assert named.returncode == 1, (
        f"a message naming an AI model returned {named.returncode}, not 1; "
        f"{named.stderr!r}"
    )
    assert "AI model" in named.stderr, (
        f"the failure output does not say what the rule is: {named.stderr!r}"
    )

    missing = run(str(_REPO_ROOT / "no-such-commit-message-file"))
    assert missing.returncode == 2, (
        f"a missing message file returned {missing.returncode}, not 2; "
        f"{missing.stderr!r}"
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
    assert "commit-msg-no-ai-model-name" in text, (
        ".pre-commit-config.yaml no longer declares the commit-msg hook "
        "under the id this gate documents. The id is what pre-commit prints "
        "when the hook fails, so a renamed id is a renamed gate."
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


def _history_messages(
    ref: str | None = None, repo: Path | None = None,
) -> list[tuple[str, str]]:
    """Return ``(sha, message)`` for every commit reachable from *ref*.

    *ref* defaults to HEAD, which is what the published-history test wants.
    The ref sweep below passes a ref name; the negative control passes a
    repository of its own. One ``git log`` per range rather than one call
    per commit, so a ref with a hundred commits does not fork a hundred
    processes.
    """
    cwd = _REPO_ROOT if repo is None else repo
    if repo is None and not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no history to check")
    cmd = ["git", "log", "--format=%H%x1f%B%x1e"]
    if ref is not None:
        cmd.append(ref)
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).stdout
    pairs = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\x1f")
        pairs.append((sha.strip(), body))
    return pairs


def _run_git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stdout.

    Synchronous by construction, so nothing it starts outlives the call.
    """
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def test_no_commit_in_this_history_carries_a_plan_id() -> None:
    """The rule is about the published history, not only future commits.

    One ``git log`` for the whole range rather than one call per commit:
    a clone with thousands of commits would otherwise fork a process each.
    """
    history = _history_messages()
    assert history, (
        "git log returned no commits — the walk is broken, and this test "
        "would pass without checking anything"
    )

    offenders: list[str] = []
    for sha, body in history:
        for v in _violations(body):
            # sha + line number + the rule's own reason.
            # NOT ``v.line``: a commit message is exactly where a private
            # plan name gets quoted, and reprinting it here puts it on the
            # operator's terminal. ``v.reason`` is safe to print because
            # the plan-id reason is built through ``redact_span``; the
            # line it names is not.
            offenders.append(f"{sha[:10]} L{v.lineno}: {v.reason}")

    assert not offenders, (
        "commits in this repository carry a plan id:\n"
        + "\n".join("    " + line for line in offenders)
        + "\n\n"
        + _PLAN_ID_REMEDY
    )


#: The fix for a plan-id finding. It is a message rewrite and it is never
#: an allowlist entry: a ref that is red for a plan id has to have the id
#: rewritten out of it, because deleting the ref does not delete the
#: commit, and a merge commit on the remote cannot be force-pushed away.
_PLAN_ID_REMEDY = (
    "For a plan-id finding: rewrite the id out of the commit message and "
    "describe the behaviour change instead. There is no allowlist entry "
    "for it — a private date-prefixed id must not sit in permanent history "
    "under a standing waiver, and deleting the ref does not delete the "
    "commit."
)


# ---------------------------------------------------------------------------
# The ref sweep — and proof that the sweep can fail
# ---------------------------------------------------------------------------


class _Scan(NamedTuple):
    """What one sweep looked at, and what it found in it.

    *refs* and *commits* are counted on the walk itself rather than
    beside it. That is the whole point of carrying them out of
    :func:`_sweep`: a surface that resolves to nothing makes every
    assertion reading the result pass without having read anything, and
    the narrower the surface is drawn the easier that is to cause by
    accident. A count taken from the same loop is the only one that
    cannot disagree with what was walked.
    """

    offenders: dict[str, dict[str, list[str]]]
    refs: int
    commits: int


def _sweep(refs: list[str], repo: Path | None = None) -> _Scan:
    """Findings on each ref, as ``{ref: {rule: [finding, ...]}}``.

    The one place the ref tests read history, so the negative controls
    below exercise the same walk the tests they guard depend on rather
    than a parallel implementation that could agree with them while both
    were broken.

    A *finding* is ``"<sha10> L<lineno>"``. The offending line itself is
    deliberately not carried through: every line the plan-id rule matches
    is by definition a line carrying a private date-prefixed id, and it
    does not belong in a failure message that is going to be pasted
    somewhere.
    """
    offenders: dict[str, dict[str, list[str]]] = {}
    walked: set[str] = set()
    for ref in refs:
        per_rule: dict[str, list[str]] = {}
        for sha, body in _history_messages(ref=ref, repo=repo):
            walked.add(sha)
            for v in _violations(body):
                per_rule.setdefault(v.rule, []).append(f"{sha[:10]} L{v.lineno}")
        if per_rule:
            offenders[ref] = per_rule
    return _Scan(offenders, len(refs), len(walked))


def _census(offenders: dict[str, dict[str, list[str]]]) -> str:
    """One line saying how many findings the report carries.

    A reader who only reads the first line of a 40-finding failure should
    still learn how much there is before the detail starts.
    """
    total = sum(len(hits) for found in offenders.values() for hits in found.values())
    if not total:
        return "no findings\n"
    return f"{total} finding(s) total\n\n"


def _report(offenders: dict[str, dict[str, list[str]]]) -> str:
    """The per-ref body of a failure message, grouped by rule.

    Shape::

        refs/heads/<name>  (2 findings)
            [plan-id] 8a66858489 L7
            [plan-id] 8a66858489 L9
    """
    blocks = []
    for ref, found in sorted(offenders.items()):
        total = sum(len(hits) for hits in found.values())
        blocks.append(
            f"  {ref}  ({total} findings)\n      "
            + "\n      ".join(
                f"[{rule}] {finding}"
                for rule in sorted(found)
                for finding in found[rule][:3]
            )
        )
    return "\n".join(blocks)


def test_the_ref_sweep_reports_a_planted_plan_id(tmp_path) -> None:
    """Negative control: the sweep below must be able to go red.

    The two tests that follow assert that no publishable ref carries a
    plan id, and that no remote-tracking ref does. Both of those can be
    satisfied by a sweep that finds nothing at all — a helper pointed at
    the wrong repository, a ref name git cannot resolve, a format string
    that silently returns zero rows. Such a helper is green forever and
    means nothing, which is the failure mode this gate was written to
    remove from the HEAD walk.

    So plant a violation where it must be found: a throwaway repository
    under ``tmp_path``, one commit whose message carries a plan id, walked
    by the same helper the sweep uses. The literal is spliced from
    fragments, as elsewhere in this file, so the control does not put the
    thing it rejects into the repository that rejects it.
    """
    repo = tmp_path / "planted-plan-id"
    repo.mkdir()
    # An empty hooks directory, so a globally configured core.hooksPath
    # cannot reject the planted commit before the test gets to walk it.
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    try:
        _run_git(repo, "init", "-q")
        (repo / "planted.txt").write_text("x\n", encoding="utf-8")
        _run_git(repo, "add", "planted.txt")
        _run_git(
            repo, "-c", "user.name=Plant", "-c", "user.email=plant@example.invalid",
            "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
            "commit", "-q", "-m", "plant a violation\n\nsee " + _PLAN_ID + " for it\n",
        )

        history = _history_messages(repo=repo)
        offenders = [
            v for _, body in history for v in _violations(body)
        ]

        assert offenders, (
            "the sweep walked a repository containing a planted plan id "
            "and reported nothing. The ref tests that follow cannot fail, "
            "so their green means only that the scan is broken."
        )
    finally:
        # Nothing started here may outlive the test: tmp_path is pytest's
        # to clean, but the repository is this test's own.
        shutil.rmtree(repo, ignore_errors=True)
    assert not repo.exists(), "the planted repository outlived the test"


def _all_refs(repo: Path | None = None) -> list[str]:
    """Every ref name git knows about, or fail: a sweep of nothing is not
    evidence of nothing."""
    cwd = _REPO_ROOT if repo is None else repo
    if repo is None and not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no refs to sweep")
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


#: The ref namespaces this gate sweeps, and what each one is.
#:
#: * ``refs/remotes/**`` — the local mirror of a remote. The receipt that
#:   a ref has already been published, and the only check that can catch
#:   a push after the fact. GitHub's pull-request head refs land here
#:   too, under whichever remote they were fetched through.
#: * ``refs/tags/**`` — a tag is pushed exactly like a branch and carries
#:   history with it, so it publishes on the same terms.
#:
#: What is deliberately **not** here is the ``refs/heads/**`` namespace.
#: Sweeping it meant sweeping every development branch, and a branch that
#: is never pushed cannot leak; the gate turned that into a finding on
#: every one of them, which is the opposite of a gate. A local branch is
#: still checked twice over — ``HEAD`` is in this surface whichever branch
#: is checked out, and anything reaching a remote has to pass the pre-push
#: hook on the way, which is the one gate that runs *before* publication
#: rather than after it.
_PUBLISHABLE_PREFIXES: tuple[str, ...] = ("refs/remotes/", "refs/tags/")


def _publishable_refs(repo: Path | None = None) -> list[str]:
    """The refs that can still be published: the surface both ref tests read.

    Two parts, in the order the report lists them:

    * ``HEAD`` — the branch being worked on, which is about to be pushed
      or merged whatever it is called. Added only when it resolves to a
      commit, so an unborn ``HEAD`` does not put a name on the surface
      that walks nothing and makes the emptiness counters read non-zero.
    * every ref under :data:`_PUBLISHABLE_PREFIXES`.
    """
    cwd = _REPO_ROOT if repo is None else repo
    if repo is None and not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no refs to sweep")
    refs: list[str] = []
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        cwd=cwd, capture_output=True, text=True,
    )
    if head.returncode == 0:
        refs.append("HEAD")
    refs.extend(r for r in _all_refs(repo=cwd) if r.startswith(_PUBLISHABLE_PREFIXES))
    return refs


#: The namespaces a published ref can live under, spelled out here rather
#: than read from :data:`_PUBLISHABLE_PREFIXES`.
#:
#: The first version of the check below imported that constant, which made
#: it useless: a deliberately broken prefix changed the constant, the check
#: looked for refs under the broken prefix, found none missing, and passed.
#: The check was asking the implementation what it ought to have found. An
#: expectation derived from the thing under test is not a check, so these two
#: strings are written out and the duplication is deliberate — if a namespace
#: is ever added, this is the second place to add it, and the mutation below
#: is what proves the two have not drifted apart.
_EXPECTED_PUBLISHABLE_PREFIXES: tuple[str, ...] = ("refs/remotes/", "refs/tags/")


def _assert_no_published_ref_was_dropped(
    refs: list[str], repo: Path | None = None
) -> None:
    """Fail unless every ref under a publishable namespace is on *refs*.

    The counters in :func:`_assert_the_sweep_was_not_empty` are totals, and
    a total is satisfied by whichever part of the surface still works. When
    the prefixes were first narrowed this assertion did not exist, and a
    deliberately broken prefix left the sweep reading ``HEAD`` alone — a
    branch whose history is clean, both counters comfortably above zero,
    and the ref test green while it no longer looked at a single published
    ref. A count cannot catch the loss of a whole namespace; enumerating
    what the namespace should have contributed can.
    """
    missing = sorted(
        r for r in _all_refs(repo=repo)
        if r.startswith(_EXPECTED_PUBLISHABLE_PREFIXES) and r not in set(refs)
    )
    assert not missing, (
        f"{len(missing)} ref(s) that can still be published are not on the "
        f"sweep surface, first {missing[:3]}. Every ref under "
        f"{_EXPECTED_PUBLISHABLE_PREFIXES} has to be swept — a surface that "
        f"quietly omits one is a gate that has stopped watching it, and the "
        f"counts in _assert_the_sweep_was_not_empty will not say so."
    )


def _assert_the_sweep_was_not_empty(scan: _Scan) -> None:
    """Fail unless the sweep actually walked something.

    Both ref tests assert that nothing was found, which a sweep of zero
    refs satisfies for the wrong reason. The narrower this surface is
    drawn, the cheaper that mistake is to make — one changed prefix and
    the gate is green because it stopped looking, not because the
    repository is clean. So the counters the walk produced itself are
    asserted non-zero before its verdict is read.

    ``test_the_narrowed_sweep_still_finds_a_pushed_ref`` plants a
    violation on the surface to show the walk is real, and empties a
    repository to show this guard is not vacuous.
    """
    assert scan.refs > 0 and scan.commits > 0, (
        f"the sweep walked {scan.refs} ref(s) and {scan.commits} commit(s). "
        f"A surface that resolves to nothing makes every assertion below "
        f"pass without checking anything: either the prefixes in "
        f"_PUBLISHABLE_PREFIXES match no ref in this repository, or "
        f"HEAD is unborn."
    )


def test_the_narrowed_sweep_still_finds_a_pushed_ref(tmp_path) -> None:
    """Negative control for the narrowing, on the surface it defines.

    Narrowing a scan surface has one failure mode that no assertion
    downstream can catch: the surface becomes empty, or stops containing
    anything reachable, and the tests that read it go green because they
    stopped looking. That is the risk this control takes on directly.

    Two halves, against throwaway repositories under ``tmp_path`` rather
    than this one — a control that planted a ref in the real repository
    to make itself red would leave it behind the first time it was
    interrupted, and the gate it guards reads every ref there.

    * A repository holding one commit whose message carries a synthetic
      plan id, published onto a ``refs/remotes/**`` ref. It must appear
      in :func:`_publishable_refs`, and the sweep must report it — so the
      narrowed surface still reaches a published ref.
    * A repository with no commits at all. The surface it yields walks
      nothing, so :func:`_assert_the_sweep_was_not_empty` must refuse it
      — so the guard both ref tests call is not itself vacuous.
    """
    planted = tmp_path / "planted-pushed"
    planted.mkdir()
    empty = tmp_path / "planted-empty"
    empty.mkdir()
    remote_ref = "refs/remotes/example/plausible-branch"
    try:
        _run_git(planted, "init", "-q")
        _run_git(planted, "read-tree", "--empty")
        tree = _run_git(planted, "write-tree").strip()
        sha = _run_git(
            planted,
            "-c", "user.name=Plant", "-c", "user.email=plant@example.invalid",
            "commit-tree", tree, "-m",
            f"a commit\n\nsee {_PLAN_ID} for the rewrite\n",
        ).strip()
        _run_git(planted, "update-ref", remote_ref, sha)

        surface = _publishable_refs(repo=planted)
        assert remote_ref in surface, (
            "a published ref is not on the surface the narrowed sweep reads: "
            f"{surface}. Either _PUBLISHABLE_PREFIXES has stopped matching "
            "refs/remotes/**, or the surface builder is not reading the "
            "repository it was given — and the sweep would then go green "
            "for having found nothing."
        )

        scan = _sweep(surface, repo=planted)
        _assert_the_sweep_was_not_empty(scan)
        assert scan.commits > 0, (
            "the sweep walked a repository holding a commit and reached "
            "none of it"
        )
        assert scan.offenders.get(remote_ref, {}).get(_RULE_PLAN_ID), (
            "a refs/remotes/** ref carrying a planted plan id was not "
            f"reported: {scan.offenders}. The surface the two ref tests "
            "read has lost the published refs, which is the one thing it "
            "was narrowed to keep."
        )

        # The other half: the guard is not a formality. This repository
        # has no commits, so nothing is reachable and the surface walks
        # nothing — the exact state in which "no offenders" would be a
        # vacuous pass rather than a clean result.
        _run_git(empty, "init", "-q")
        empty_scan = _sweep(_publishable_refs(repo=empty), repo=empty)
        assert empty_scan.commits == 0, (
            f"a repository with no commits reported {empty_scan.commits} "
            "walked commit(s); the fixture is not exercising the empty case"
        )
        with pytest.raises(AssertionError, match="commit"):
            _assert_the_sweep_was_not_empty(empty_scan)
    finally:
        # Nothing started here may outlive the test: tmp_path is pytest's
        # to clean, but the repositories are this test's own.
        shutil.rmtree(planted, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)
    assert not planted.exists(), "the planted repository outlived the test"
    assert not empty.exists(), "the planted repository outlived the test"


def test_no_publishable_ref_carries_a_plan_id() -> None:
    """Every ref that can still be published, and not one branch more.

    ``test_no_commit_in_this_history_carries_a_plan_id`` is green and can
    be misread as *this repository has no plan-id-bearing history*. It is
    a statement about one ref.

    The surface used to be every ref ``git for-each-ref`` would name,
    which swept in every development branch as well. That is not a wider
    net for the thing this gate is about, it is a different question: a
    branch nobody pushes cannot leak, and reporting one turns a finding
    into background noise that trains the reader to ignore the report.
    What is left is the set of refs that can still become public — the
    branch in hand, the remote mirrors and the tags — and those are what
    this asserts on. See :func:`_publishable_refs` for why each part is
    in it.
    """
    refs = _publishable_refs()

    # Every ref under the publishable prefixes has to be on the surface.
    # Read as a set rather than left to the counters below, which a single
    # surviving part of the surface satisfies on its own.
    _assert_no_published_ref_was_dropped(refs)

    scan = _sweep(refs)
    _assert_the_sweep_was_not_empty(scan)
    offenders = scan.offenders
    # sha + line number only. NOT ``v.line``: the ref sweep reads
    # commit bodies on refs that a rewrite left behind, and those bodies
    # quote the private plan name in prose. Reprinting them is how this
    # assertion painted them across the terminal the first time it ran
    # red. ``_sweep`` therefore carries the location only -- see its
    # docstring.
    assert not offenders, (
        "refs that can still be published retain commits carrying a plan "
        "id:\n"
        + _census(offenders)
        + _report(offenders)
        + "\n\n"
        + _PLAN_ID_REMEDY
    )


def test_no_remote_tracking_ref_carries_a_plan_id() -> None:
    """The push rule, in the one form that can be checked after the fact.

    A remote-tracking ref is the receipt: if ``refs/remotes/**`` carries a
    violation, the plan id is already on the remote, and stripping it
    locally has not undone that.

    Read from the same surface the test above reads, rather than from
    ``git for-each-ref`` directly, so the two cannot come to disagree
    about which refs are in scope.
    """
    remote_refs = [r for r in _publishable_refs() if r.startswith("refs/remotes/")]
    if not remote_refs:
        # Vacuous unless a remote is configured and simply not fetched —
        # that is a broken scan, not an absence of pushes.
        configured = subprocess.run(
            ["git", "remote"], cwd=_REPO_ROOT, capture_output=True, text=True,
        ).stdout.split()
        assert not configured, (
            "remotes are configured but git enumerated no refs/remotes/** "
            "ref — the scan is broken, so this test cannot detect a push"
        )

    scan = _sweep(remote_refs)
    _assert_the_sweep_was_not_empty(scan)
    offenders = scan.offenders
    # sha + line number only. NOT ``v.line`` — see the comment in the
    # test above; ``_sweep`` carries the location only.
    assert not offenders, (
        "remote-tracking refs carry commits with a plan id in them, so "
        "that history has been published:\n"
        + _census(offenders)
        + _report(offenders)
        + "\n\n"
        + _PLAN_ID_REMEDY
        + "\n\nNone of this can be undone by rewriting local history; the "
        "remote has to be dealt with, and nothing already fetched can be "
        "recalled."
    )
