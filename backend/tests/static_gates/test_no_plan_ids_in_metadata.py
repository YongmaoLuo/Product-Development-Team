"""No commit message, branch name, or PR title/body may carry a plan-id shape.

Why this rule is separate from the source rule
---------------------------------------------
:mod:`test_no_plan_ids_in_non_test_source` has to make a judgement call,
because a date-prefixed id in a test file is usually a *fixture* and there
are hundreds of those. On these surfaces there is no such excuse: nothing
under ``tests/`` is reachable from a commit message, a branch name, or a pull
request body. A plan id appearing in any of them is one that was typed by hand,
every time, on purpose.

So the rule is a straight application of the shape with **zero exemptions**
and therefore **zero false positives by construction**. That is the whole
reason it can be enforced at commit time, where a refusal costs a rewrite.

Why these surfaces matter more than the tree
--------------------------------------------
GitHub composes the merge commit on ``main`` itself::

    Merge pull request #27 from YongmaoLuo/<branch>

    <title>

    <body>

Every word of that becomes a public, permanent commit. ``pr_metadata_scan``
already says so: the branch name and the PR title are inside the message
and no tree gate reads either one. This rule closes the commit-message end
of the same gap.

Measured on this branch, the leaked plan ids reach the message surface too:
across every ref, four commit messages carry a plan-id shape and three of
them name the same private remote.

Where it runs
-------------
One definition, four callers, so the answer cannot drift:

  * ``commit_range_scan`` -- every commit message in a pushed range.
  * ``pr_metadata_scan`` -- the branch name, title and body.
  * ``scripts/check_commit_msg.py`` -- the ``commit-msg`` hook, i.e. before
    the commit exists at all.
  * the ``commit-msg`` hook in ``.pre-commit-config.yaml``.

Why this file must not import pytest
-------------------------------------
``scripts/check_commit_msg.py`` runs under a bare ``python3`` from the
``commit-msg`` hook -- no venv, no third-party packages -- and imports
:func:`find_plan_ids_in_metadata` from here. A top-level ``import pytest``
would break that hook with an ImportError. The tests below use plain
``assert``.
"""

from __future__ import annotations

from pathlib import Path

import source_scan
from test_no_plan_ids_in_non_test_source import PLAN_ID_SHAPE

#: The synthetic label used when a caller has no natural path for the
#: surface it is scanning. A branch name has no file behind it, and a
#: commit message has neither; naming them keeps the ``(path, text)``
#: signature every rule in ``RANGE_RULES`` and ``METADATA_RULES`` shares.
COMMIT_MESSAGE_LABEL = Path("commit message")
BRANCH_NAME_LABEL = Path("branch name")


def find_plan_ids_in_metadata(
    label: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``(label, lineno, plan_id)`` for every plan id in *text*.

    *label* names the surface -- ``Path("commit message")``,
    ``Path("pull_request/title")``, ``Path("branch name")``. It is echoed
    back in every finding so a consumer can say which surface failed
    without threading it through separately.

    No path is exempt and no text is special-cased: on these surfaces a
    date-prefixed identifier has no synthetic reason to exist, so the
    function is the shape and nothing else.
    """
    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen: set[str] = set()
        for match in PLAN_ID_SHAPE.finditer(line):
            plan_id = match.group(0)
            if plan_id in seen:
                continue
            seen.add(plan_id)
            hits.append((label, lineno, plan_id))
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_is_non_empty() -> None:
    """The gate directory must still hold scannable source.

    Guards the import above from silently becoming a no-op if the sibling
    module is ever removed: the scan target itself has to exist.
    """
    assert list(source_scan.iter_first_party_sources(roots=(Path("."),))), (
        "no first-party source found next to this file — the rule this "
        "module reuses may have moved."
    )


def test_a_leaked_commit_message_is_rejected() -> None:
    """A real leaked subject is caught. Fragments, so this file stays clean."""
    message = (
        "refactor(provider): 跟着实时配额走\n"
        "\n"
        "事故：plan " + "2026" + "1010-" + "CC-Switch-" + "Remote-Aut" + " 93 条 repair-* 行里 36 条停在\n"
    )
    hits = find_plan_ids_in_metadata(COMMIT_MESSAGE_LABEL, message)
    assert [h[2] for h in hits] == ["2026" + "1010-" + "CC-Switch-" + "Remote-Aut"], hits
    assert hits[0][1] == 3, hits


def test_every_real_leaked_message_is_rejected() -> None:
    """The four leaked message lines found across every ref on this repo.

    Each is a verbatim shape from a real commit message; assembled from
    fragments so this file does not reproduce them.
    """
    bodies = [
        "`%s` 上：21 个 VP 里 0 个有 test_command" % ("2026" + "1010-" + "CC-Switch-" + "Remote-Aut"),
        "事故：plan %s 93 条 repair-* 行里 36 条停在" % ("2026" + "1010-" + "CC-Switch-" + "Remote-Aut"),
        "只有 %s 的 repair-r1-01 被真删掉" % ("2026" + "1012-" + "chan" + "lun"),
        "%s 一役暴露的问题：该 plan 的任务数在" % ("2026" + "1010-" + "CC-Switch-" + "Remote-Aut"),
    ]
    for body in bodies:
        assert find_plan_ids_in_metadata(COMMIT_MESSAGE_LABEL, body), body


def test_a_leaked_branch_name_is_rejected() -> None:
    """A branch name reaches the merge commit message on ``main``."""
    branch = "fix/" + "2026" + "1012-" + "chan" + "lun-buy" + "sell-points"
    hits = find_plan_ids_in_metadata(BRANCH_NAME_LABEL, branch)
    assert [h[2] for h in hits] == ["2026" + "1012-" + "chan" + "lun-buy" + "sell-points"], hits


def test_a_leaked_pull_request_body_is_rejected() -> None:
    """The body is the one field that can hold arbitrary prose."""
    body = "Reproduced on the real plan %s, see the trace." % ("2026" + "0926-" + "Product-" + "Development")
    hits = find_plan_ids_in_metadata(Path("pull_request/body"), body)
    assert len(hits) == 1, hits


def test_there_are_no_exemptions_here() -> None:
    """The neutral placeholder is a hit here too.

    On a source tree the placeholder is arguable; on a commit message there
    is nothing to argue about, and allowing it would make the rule's
    "zero false positives by construction" claim untrue.
    """
    text = "use %s as the plan id\n" % ("2026" + "0101-example")
    assert find_plan_ids_in_metadata(COMMIT_MESSAGE_LABEL, text)


def test_ordinary_commit_messages_are_accepted() -> None:
    """This repository's real subjects must pass unchanged."""
    messages = [
        "fix(verification): 作废要真的生效 —— apply_delta 透传判据载荷、replay 认 obsolete",
        "refactor(env): 去掉三个假的必填项 —— 没有任何代码读它们",
        "test(suite): 拆掉单元套件里的隐式顺序耦合 —— 配对测试搬进独立会话",
        "Merge pull request #40 from YongmaoLuo/refactor/one-dotenv-at-the-root",
    ]
    for message in messages:
        assert find_plan_ids_in_metadata(COMMIT_MESSAGE_LABEL, message) == [], message
