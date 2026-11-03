"""Pins for the orphan-reaper operations pages.

What is under test
------------------
``docs/operations/orphan-reaper.md`` and ``docs/operations/orphan-reaper.zh.md``
-- the two languages of the reaper's operator contract.

The reaper grew flags and an environment variable that the contract did
not mention yet: the census of the machine's daemons (``--status``), the
owner contract (``--owner-pid`` / ``--owner-start`` /
``--survive-owner``) and the machine-wide ceiling
(``PDT_ORPHAN_REAPER_MAX_DAEMONS``).  A flag that is implemented but not
documented is a flag nobody can turn off, and an environment variable
that is not documented is a ceiling nobody knows they can raise.

Why the pages are pinned rather than trusted
--------------------------------------------
The pages are prose, and prose cannot be tested for truth.  What *can* be
tested is that the contract still carries what it is supposed to carry:
the new flags and the new variable, in **both** languages, and the three
rules that decide when a daemon goes away.  A page pair that has drifted
is a pair where one language is quietly out of date, and the reader of
that language is the one who gets hurt.

The pins
--------
1. The English page names the four new tokens.
2. The Chinese page names the four new tokens.
3. Both pages carry the owner contract, the root-rebuild rule and the
   ceiling -- each in its own section, with the vocabulary the
   implementation itself uses.
4. The two pages carry the same tokens, so neither language has fallen
   behind.
5. The verdict ladder is still the verdict ladder: six rungs, and the
   reason each one records.  The lifecycle sections are *about* the
   daemon; they are not allowed to redefine which processes die.
6. ``--once`` still says what it says.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

#: The repository root -- ``<repo>``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The English page of the reaper's operator contract.
_EN_DOC = _REPO_ROOT / "docs" / "operations" / "orphan-reaper.md"

#: The Chinese page of the same contract.
_ZH_DOC = _REPO_ROOT / "docs" / "operations" / "orphan-reaper.zh.md"

#: The tokens the two pages have to name.  The census, the owner pid,
#: the owner exemption and the machine-wide ceiling: the four names an
#: operator needs to find in this page.
_REQUIRED_TOKENS: Tuple[str, ...] = (
    "--status",
    "--owner-pid",
    "--survive-owner",
    "PDT_ORPHAN_REAPER_MAX_DAEMONS",
)

#: The owner's start time -- the other half of the owner contract.  A
#: pid on its own is not an identity, and the page has to say so.
_OWNER_START_TOKEN = "--owner-start"

#: The three rules that decide when a daemon goes away, as sections.
#: The English page and the Chinese page each carry their own heading
#: for each of them.
_LIFECYCLE_SECTIONS: Dict[str, Tuple[str, str]] = {
    "en": (
        "The owner dies",
        "The root is rebuilt",
        "The machine's ceiling",
    ),
    "zh": (
        "owner 死亡",
        "root 被重建",
        "整机的上限",
    ),
}

#: The census section -- the observability half of the contract.
_CENSUS_SECTION: Dict[str, str] = {
    "en": "The census",
    "zh": "守护进程清单",
}

#: What each lifecycle section has to say, per language.  The phrases
#: are the implementation's own words: the log lines an operator greps
#: for, and the vocabulary the code uses.
_SECTION_REQUIREMENTS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "en": {
        "The owner dies": (
            "--owner-pid",
            "--owner-start",
            "--survive-owner",
            "is gone; leaving",
        ),
        "The root is rebuilt": (
            "inode",
            "has been replaced; leaving",
        ),
        "The machine's ceiling": (
            "PDT_ORPHAN_REAPER_MAX_DAEMONS",
            "standing down",
            "lock",
        ),
        "The census": (
            "--status",
            "count",
            "daemons",
        ),
    },
    "zh": {
        "owner 死亡": (
            "--owner-pid",
            "--owner-start",
            "--survive-owner",
            "is gone; leaving",
        ),
        "root 被重建": (
            "inode",
            "has been replaced; leaving",
        ),
        "整机的上限": (
            "PDT_ORPHAN_REAPER_MAX_DAEMONS",
            "standing down",
            "lock",
        ),
        "守护进程清单": (
            "--status",
            "count",
            "daemons",
        ),
    },
}

#: The six rungs of the verdict ladder, and the reason each one records.
#: The lifecycle sections are about the daemon's own life; the ladder is
#: about which processes die, and it is not allowed to change.
_LADDER_REASONS: Tuple[str, ...] = (
    "unmarked",
    "owner_alive",
    "pid_reused",
    "protected_port",
    "self_reaper",
    "orphan",
)

#: The six rung headings of the English page.
_EN_LADDER_HEADINGS: Tuple[str, ...] = (
    "### 1. No PDT marker",
    "### 2. Owner still alive",
    "### 3. PID reuse",
    "### 4. Protected port",
    "### 5. The reaper itself",
    "### 6. Owner is dead — reclaim",
)

#: The six rung headings of the Chinese page.
_ZH_LADDER_HEADINGS: Tuple[str, ...] = (
    "### 1. 无 PDT 标记",
    "### 2. owner 存活",
    "### 3. PID 复用",
    "### 4. 保护端口",
    "### 5. reaper 自身",
    "### 6. owner 已死 → 回收",
)


def _read(path: Path) -> str:
    """The page's text."""
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the ``heading`` section, up to the next heading.

    Returns the empty string when there is no such section, so a
    missing section fails the way a missing requirement should.
    """
    pattern = re.compile(
        r"^#{2,3}\s+" + re.escape(heading) + r"\s*$(.*?)(?=^#{1,3}\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    return match.group(1)


def _tokens_in(text: str) -> List[str]:
    """The new tokens *text* names, in a fixed order."""
    return [token for token in _REQUIRED_TOKENS if token in text]


# ---------------------------------------------------------------------------
# 1. The English page names the new tokens
# ---------------------------------------------------------------------------


def test_english_doc_covers_new_flags():
    """The English contract names the census, the owner and the ceiling.

    A flag that the page does not mention is a flag an operator cannot
    find, so the four names have to be on the page.
    """
    text = _read(_EN_DOC)
    missing = [token for token in _REQUIRED_TOKENS if token not in text]
    assert not missing, (
        f"the English page does not name {missing!r} -- the reaper's "
        "daemon lifecycle is implemented but not documented"
    )


# ---------------------------------------------------------------------------
# 2. The Chinese page names the new tokens
# ---------------------------------------------------------------------------


def test_chinese_doc_covers_new_flags():
    """The Chinese contract names the same four names.

    The pages are a pair: a reader of one language must not be the only
    one who cannot find the flag.
    """
    text = _read(_ZH_DOC)
    missing = [token for token in _REQUIRED_TOKENS if token not in text]
    assert not missing, (
        f"the Chinese page does not name {missing!r} -- the two pages "
        "have drifted apart"
    )


# ---------------------------------------------------------------------------
# 3. Both pages carry the three lifecycle rules
# ---------------------------------------------------------------------------


def test_english_doc_covers_daemon_lifecycle():
    """The English page has a section for each lifecycle rule.

    The rules are the ones an operator needs at 3am: the owner died,
    the root was rebuilt, the machine is full.
    """
    text = _read(_EN_DOC)
    for heading in _LIFECYCLE_SECTIONS["en"] + (_CENSUS_SECTION["en"],):
        assert _section(text, heading).strip(), (
            f"the English page has no '{heading}' section"
        )
    for heading, required in _SECTION_REQUIREMENTS["en"].items():
        body = _section(text, heading)
        assert body.strip(), f"the English page has no '{heading}' section"
        for phrase in required:
            assert phrase in body, (
                f"the English '{heading}' section does not mention {phrase!r}"
            )


def test_chinese_doc_covers_daemon_lifecycle():
    """The Chinese page has a section for each lifecycle rule."""
    text = _read(_ZH_DOC)
    for heading in _LIFECYCLE_SECTIONS["zh"] + (_CENSUS_SECTION["zh"],):
        assert _section(text, heading).strip(), (
            f"the Chinese page has no '{heading}' section"
        )
    for heading, required in _SECTION_REQUIREMENTS["zh"].items():
        body = _section(text, heading)
        assert body.strip(), f"the Chinese page has no '{heading}' section"
        for phrase in required:
            assert phrase in body, (
                f"the Chinese '{heading}' section does not mention {phrase!r}"
            )


def test_both_pages_name_the_owner_start_time():
    """The owner is a pid *and* a start time, on both pages.

    A pid on its own is not an identity -- the kernel recycles pids --
    so the page that documents the owner contract has to document the
    whole of it.
    """
    for path in (_EN_DOC, _ZH_DOC):
        text = _read(path)
        assert _OWNER_START_TOKEN in text, (
            f"{path.name} does not name {_OWNER_START_TOKEN!r}"
        )


# ---------------------------------------------------------------------------
# 4. The two pages carry the same tokens
# ---------------------------------------------------------------------------


def test_the_two_pages_carry_the_same_tokens():
    """Neither language has fallen behind the other.

    The four names are the contract's vocabulary; a page that carries
    three of them is a page whose reader is missing one.
    """
    english = _tokens_in(_read(_EN_DOC))
    chinese = _tokens_in(_read(_ZH_DOC))
    assert english == chinese == list(_REQUIRED_TOKENS), (
        f"the two pages carry different tokens: {english!r} vs {chinese!r}"
    )


# ---------------------------------------------------------------------------
# 5. The verdict ladder has not moved
# ---------------------------------------------------------------------------


def test_the_verdict_ladder_is_still_six_rungs():
    """The lifecycle sections did not touch the ladder.

    The ladder is the only thing in the reaper that can kill a process.
    The new sections are about the daemon's own lifetime; they are not
    allowed to redefine which processes die.
    """
    for path, headings in ((_EN_DOC, _EN_LADDER_HEADINGS), (_ZH_DOC, _ZH_LADDER_HEADINGS)):
        text = _read(path)
        for heading in headings:
            assert heading in text, f"{path.name} has no '{heading}' rung"
        for reason in _LADDER_REASONS:
            assert reason in text, (
                f"{path.name} no longer records the reason {reason!r}"
            )


# ---------------------------------------------------------------------------
# 6. --once still says what it says
# ---------------------------------------------------------------------------


def test_once_semantics_is_unchanged():
    """``--once`` is still one pass, then exit.

    The new modes are additions; they must not have changed what
    ``--once`` does.
    """
    english = _read(_EN_DOC)
    assert "| `--once` | one pass: scan, reap, write the events down, " \
        "print one line per verdict, and exit |" in english, (
        "the English page no longer describes --once as one pass"
    )
    chinese = _read(_ZH_DOC)
    assert "| `--once` | 跑一轮：扫描、回收、把事件记下来、每个判定打印一行，然后退出 |" \
        in chinese, "the Chinese page no longer describes --once as one pass"
