"""Both changelogs have to carry the judgment-semantics section.

Why this file exists
--------------------
The judgment-surface material (§7, §12.3) makes the changelog the place
where a change in *judgment semantics* is recorded, and it makes that
place a fixed one: a ``### 判定语义变更`` section under
``## [Unreleased]``, in **both** languages, whether there is anything to
say or not ("无（本 PR 未改判定面）").

The accident that started the work (§1) was a 137-line change to the
judgment semantics that landed inside an unrelated pull request: the
changelog said nothing about it, and the long-running process was still
holding the old module in memory, so four rounds judged by the old rule.
A rule nobody can see is not a rule — so the section has to be somewhere
a reader is guaranteed to look, and the entry in it has to name enough
to be audited afterwards.

What it pins
------------
1. **Both changelogs carry the section, inside the ``[Unreleased]``
   block.** A section filed under a released version, or in one language
   only, is invisible to the reader of the current release — and a gate
   that only greps the whole file for the heading would still pass.
2. **The backfill names what it backfills**: commit ``88d898f9`` and the
   functions whose meaning it changed. An entry that says "a judgment
   changed" without naming the commit cannot be audited with
   ``git log`` — which is exactly how the accident was dug out of
   ``88d898f9`` in the first place.
3. **The two languages name the same things.** The prose around a name
   is translated; the name itself is not — the same rule the bilingual
   orphan-reaper gate follows. A reader of one language has to learn
   about a judgment change the other language does not mention.

The cases below the three are the **negative controls**: each takes a
changelog that has been broken on purpose and requires the checks above
to *name the offender*. Without them, a check that reported nothing
would pass the cases as happily as it passes a correct changelog — and
a gate that cannot go red is not a gate.

Scope, stated honestly: this gate reads the **text** of the two
changelogs. It cannot judge whether the entry is complete — that is a
review job, and §7 says so too.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The repository root — three levels up from ``backend/tests/static_gates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two changelogs, in the two languages they are written in.
_CHANGELOGS = (
    _REPO_ROOT / "CHANGELOG.md",
    _REPO_ROOT / "CHANGELOG.zh.md",
)

#: The heading the material pins (§7, §12.3).  It is spelled the same in
#: both files on purpose: the CI gate reads one string out of both
#: changelogs, and a section that exists in only one of them is a
#: section half the readers never see.
_SECTION_HEADING = "### 判定语义变更"

#: The block the section has to live in.
_UNRELEASED_HEADING = "## [Unreleased]"

#: A level-2 heading opens the next release.
_RELEASE_HEADING = re.compile(r"^## \[", re.MULTILINE)

#: A level-3 heading ends the section.
_SUBSECTION_HEADING = re.compile(r"^### ", re.MULTILINE)

#: The commit whose judgment change was never recorded (§1.3).
_BACKFILL_COMMIT = "88d898f9"

#: The functions that commit changed the meaning of: the exemption
#: lives in the first, the gate it exempts is the second.
_BACKFILL_FUNCTIONS = (
    "_ran_the_declared_entry",
    "verdict_subject_mismatch",
)

#: The module the change lives in — the file a reader has to open.
_BACKFILL_MODULE = "backend/verification_evidence.py"

#: A commit hash, written bare or inside backticks.
_COMMIT = re.compile(r"\b[0-9a-f]{7,40}\b")

#: A name written inside backticks.
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _unreleased_block(text: str) -> str:
    """The text between ``## [Unreleased]`` and the next release heading.

    Empty when the file carries no ``## [Unreleased]`` block at all.
    """
    start = text.find(_UNRELEASED_HEADING)
    if start == -1:
        return ""
    body = start + len(_UNRELEASED_HEADING)
    match = _RELEASE_HEADING.search(text, body)
    end = match.start() if match else len(text)
    return text[body:end]


def _judgment_section(text: str) -> str:
    """The ``### 判定语义变更`` section inside the ``[Unreleased]`` block.

    Empty when the block does not carry the section.  A section filed
    *below* the next release heading is not in the block and is therefore
    not found — which is the point: the section has to be where a reader
    looks for the current release.
    """
    block = _unreleased_block(text)
    start = block.find(_SECTION_HEADING)
    if start == -1:
        return ""
    body = start + len(_SECTION_HEADING)
    match = _SUBSECTION_HEADING.search(block, body)
    end = match.start() if match else len(block)
    return block[start:end]


def _named_tokens(section: str) -> set[str]:
    """The names a section carries.

    Translated prose around them, untranslated names inside them — the
    rule the bilingual orphan-reaper gate follows.  These are the names
    that have to match across the two languages: a commit, a function, a
    module path.
    """
    tokens = set(_BACKTICKED.findall(section))
    tokens.update(_COMMIT.findall(section))
    return tokens


def _section_problems(text: str) -> list[str]:
    """Why ``text`` does not carry the section, in the unreleased block."""
    if not _unreleased_block(text):
        return ["the file has no `## [Unreleased]` block"]

    section = _judgment_section(text)
    if not section:
        return [
            f"the `[Unreleased]` block carries no `{_SECTION_HEADING}` section"
        ]

    problems: list[str] = []
    if _BACKFILL_COMMIT not in section:
        problems.append(f"the section does not name `{_BACKFILL_COMMIT}`")
    for function in _BACKFILL_FUNCTIONS:
        if function not in section:
            problems.append(f"the section does not name `{function}`")
    if _BACKFILL_MODULE not in section:
        problems.append(f"the section does not name `{_BACKFILL_MODULE}`")
    return problems


def _read(path: Path) -> str:
    """The changelog at ``path``."""
    assert path.is_file(), f"{path} is missing: there is no changelog to check"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The three cases the material names
# ---------------------------------------------------------------------------


def test_both_changelogs_have_unreleased_judgment_section() -> None:
    """Both changelogs carry the section, inside the ``[Unreleased]`` block.

    One language only is half a record: the reader of the other one
    learns nothing, and the section that exists is the one nobody
    reads.  A section filed under a released version is worse — it says
    the current release says nothing.
    """
    for path in _CHANGELOGS:
        text = _read(path)

        section = _judgment_section(text)

        assert section, (
            f"{path.name} has no `{_SECTION_HEADING}` section between "
            f"`{_UNRELEASED_HEADING}` and the next release heading"
        )


def test_backfill_entry_names_88d898f9() -> None:
    """The entry names the commit and the functions it changed.

    Without the commit there is nothing to audit: the entry claims a
    judgment changed and leaves the reader no way to find out which
    change it was.  Without the function names the entry names no
    behavior at all.
    """
    for path in _CHANGELOGS:
        text = _read(path)

        section = _judgment_section(text)

        assert _BACKFILL_COMMIT in section, (
            f"{path.name}'s `{_SECTION_HEADING}` section does not name "
            f"`{_BACKFILL_COMMIT}` — the backfill has to say what it is "
            "backfilling"
        )
        for function in _BACKFILL_FUNCTIONS:
            assert function in section, (
                f"{path.name}'s `{_SECTION_HEADING}` section does not name "
                f"`{function}` — a judgment change that names no function "
                "names no behavior"
            )


def test_both_sections_name_the_same_things() -> None:
    """The two languages name the same names.

    The prose is translated — the names are not.  If one language names
    a commit the other does not, the reader of that language learns
    about a judgment change the other reader never hears about, and
    the two files stop being one record in two languages.
    """
    sections = {
        path.name: _judgment_section(_read(path)) for path in _CHANGELOGS
    }

    tokens = {name: _named_tokens(section) for name, section in sections.items()}

    for name, named in tokens.items():
        assert named, f"{name}'s section names nothing at all"

    english, chinese = (
        tokens["CHANGELOG.md"],
        tokens["CHANGELOG.zh.md"],
    )

    assert english == chinese, (
        "the two changelogs do not name the same things: "
        f"only in CHANGELOG.md {sorted(english - chinese)}, "
        f"only in CHANGELOG.zh.md {sorted(chinese - english)}"
    )


# ---------------------------------------------------------------------------
# The negative controls: each check has to be able to go red
# ---------------------------------------------------------------------------


def _minimal_changelog() -> str:
    """A changelog with an unreleased block and nothing in it."""
    return (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n\n"
        "- Nothing yet.\n\n"
        "## [0.1.2] - 2026-10-07\n\n"
        "### Added\n\n"
        "- Old news.\n"
    )


def test_a_changelog_without_the_section_is_reported() -> None:
    """A changelog with no judgment section is reported, not accepted."""
    problems = _section_problems(_minimal_changelog())

    assert problems, "a changelog without the section passed the check"
    assert _SECTION_HEADING in problems[0]


def test_a_section_below_the_next_release_is_reported() -> None:
    """A section filed under a released version is reported."""
    text = _minimal_changelog().replace(
        "## [0.1.2] - 2026-10-07",
        f"## [0.1.2] - 2026-10-07\n\n{_SECTION_HEADING}\n\n- Too late.\n",
    )

    problems = _section_problems(text)

    assert problems, (
        "a judgment section filed below the next release passed the check"
    )


def test_a_backfill_without_the_commit_is_reported() -> None:
    """An entry that names no commit is reported."""
    text = _minimal_changelog().replace(
        "## [0.1.2] - 2026-10-07",
        (
            f"{_SECTION_HEADING}\n\n"
            f"- `{_BACKFILL_FUNCTIONS[0]}` changed.\n\n"
            "## [0.1.2] - 2026-10-07"
        ),
    )

    problems = _section_problems(text)

    assert problems, "a backfill naming no commit passed the check"
    assert _BACKFILL_COMMIT in problems[0]


def test_a_backfill_naming_no_function_is_reported() -> None:
    """An entry that names no function is reported."""
    text = _minimal_changelog().replace(
        "## [0.1.2] - 2026-10-07",
        (
            f"{_SECTION_HEADING}\n\n"
            f"- Something changed ({_BACKFILL_COMMIT}).\n\n"
            "## [0.1.2] - 2026-10-07"
        ),
    )

    problems = _section_problems(text)

    assert problems, "a backfill naming no function passed the check"
    assert any(_BACKFILL_FUNCTIONS[0] in problem for problem in problems)
