"""The Chinese orphan reaper page has to be a page, not a placeholder.

Why this file exists
--------------------
The bilingual gate (``backend/tests/static_gates/test_docs_are_bilingual.py``)
checks that a ``.zh.md`` sibling *exists*.  It does not check that the page
says anything: a body consisting of a single title line satisfies it, and
the gate was already green before this file was written.  So it cannot carry
the claim that the Chinese page is a translation.

The orphan reaper is the one component in this system that sends signals to
processes it did not start in the current round.  Its safety does not come
from the code being careful — it comes from rules an operator can *read*:
which variables turn it off, and the six rungs of the verdict ladder that
separate "ours, and orphaned" from everything else.  An operator reading the
Chinese page has to get the same contract as one reading the English page,
so the Chinese page has to name the same five environment variables and
carry the same six numbered rungs.

Scope, stated honestly
----------------------
What is checked here is that the *names* and the *section structure* are
present: the five variables spelled exactly as an operator types them, and
six separately numbered sections with real prose under each.  Whether the
Chinese prose is a faithful translation of the English one is a job for a
reader, not for a regex.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The repository root — three levels up from ``backend/tests/unit``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The Chinese page under test.
_ZH_DOC = _REPO_ROOT / "docs" / "operations" / "orphan-reaper.zh.md"

#: The English page it mirrors.
_EN_DOC = _REPO_ROOT / "docs" / "operations" / "orphan-reaper.md"

#: The five configuration variables an operator may need to set.
#:
#: Their names, defaults and meanings are the ones
#: ``backend/orphan_reaper.py`` resolves them from.  A variable name is what
#: an operator types, so it is spelled the same way in every language —
#: translated prose around it, untranslated name inside it.
REQUIRED_ENV_VARS = (
    "PDT_ORPHAN_REAPER",
    "PDT_ORPHAN_REAPER_INTERVAL",
    "PDT_ORPHAN_REAPER_PROTECT_PORTS",
    "PDT_ORPHAN_REAPER_GRACE",
    "PDT_ORPHAN_REAPER_IDLE_EXIT",
)

#: The six rungs of the verdict ladder, in the order they are walked.
#:
#: The English page numbers them 1–6, and the Chinese page mirrors that
#: numbering, so a reader who has read one can find the corresponding rung
#: in the other.  Each entry is ``(number, title fragment)``; the fragment
#: is the Chinese wording of the rung, so a page that numbered its sections
#: but put the wrong one under a number does not pass.
LADDER_STEPS = (
    (1, "无 PDT 标记"),
    (2, "owner 存活"),
    (3, "PID 复用"),
    (4, "保护端口"),
    (5, "reaper 自身"),
    (6, "owner 已死"),
)

#: How much prose the Chinese page must carry below its title.
#:
#: The whole point of this file is that "a page exists" is not the claim —
#: "a page says something" is.  A single title line is the failure mode the
#: bilingual gate cannot see, so the body has to be substantial enough that
#: it cannot be satisfied by a stub.
_MIN_BODY_CHARS = 200


def _sections(text: str) -> list[tuple[int, str, str]]:
    """Split a Markdown page into ``(heading level, title, body)`` sections.

    A heading is a line starting with ``#``; its body is everything up to
    the next heading of any level.  The body is what separates a section
    from a placeholder — a heading with nothing under it is an outline, not
    a page.  Fenced code blocks are skipped, so a ``#`` comment inside a
    shell snippet is not mistaken for a heading.
    """
    sections: list[tuple[int, str, str]] = []
    level: int | None = None
    title = ""
    body: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            body.append(line)
            continue
        match = None if in_fence else re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if level is not None:
                sections.append((level, title, "\n".join(body).strip()))
            level = len(match.group(1))
            title = match.group(2).strip()
            body = []
        elif level is not None:
            body.append(line)

    if level is not None:
        sections.append((level, title, "\n".join(body).strip()))
    return sections


def _zh_text() -> str:
    """Return the Chinese page's text."""
    return _ZH_DOC.read_text(encoding="utf-8")


def test_chinese_doc_exists() -> None:
    """The Chinese page exists, and it carries prose rather than a title."""
    assert _ZH_DOC.is_file(), (
        f"{_ZH_DOC.relative_to(_REPO_ROOT)} does not exist — a reader who "
        "switches the site to Chinese falls back to English, and the "
        "bilingual gate cannot see that the fallback happened"
    )

    text = _zh_text()
    sections = _sections(text)
    assert sections, f"{_ZH_DOC.relative_to(_REPO_ROOT)} is empty"

    assert sections[0][0] == 1, (
        "the Chinese page does not start with a top-level title"
    )

    body_chars = sum(len(body) for _, _, body in sections)
    assert body_chars >= _MIN_BODY_CHARS, (
        f"the Chinese page carries only {body_chars} characters of body "
        f"text (the gate wants at least {_MIN_BODY_CHARS}). A page whose "
        "body is a title and nothing else would satisfy the bilingual gate "
        "while telling a Chinese reader nothing."
    )


def test_chinese_doc_repeats_env_names() -> None:
    """Every variable an operator types has to appear, spelled the same way."""
    text = _zh_text()

    missing = [name for name in REQUIRED_ENV_VARS if name not in text]
    assert not missing, (
        "the Chinese orphan reaper page does not mention these "
        "configuration variables, so a Chinese-reading operator cannot find "
        "out how to configure the reaper: " + ", ".join(missing)
    )


def test_chinese_doc_covers_all_six_ladder_steps() -> None:
    """The six rungs are separate, numbered sections, each with prose."""
    sections = _sections(_zh_text())

    for number, fragment in LADDER_STEPS:
        pattern = re.compile(r"^%d\.\s*%s" % (number, re.escape(fragment)))
        matching = [
            (title, body)
            for level, title, body in sections
            if level == 3 and pattern.match(title)
        ]
        assert matching, (
            f"the Chinese orphan reaper page has no `### {number}. {fragment}` "
            "section — the verdict ladder has to be numbered in order, so "
            "that the two pages can be read side by side"
        )
        assert matching[0][1], (
            f"the `### {number}. {fragment}` section of the Chinese page has "
            "no body — a heading on its own is an outline, not a page"
        )

    # Each rung is its own section: the six headings are six distinct
    # sections of the page, in the order they are walked.
    ladder = [
        (level, title)
        for level, title, _ in sections
        if level == 3
        and any(
            re.match(r"^%d\.\s*%s" % (number, re.escape(fragment)), title)
            for number, fragment in LADDER_STEPS
        )
    ]
    assert len(ladder) == len(LADDER_STEPS), (
        f"the Chinese page has {len(ladder)} numbered ladder sections, "
        f"expected {len(LADDER_STEPS)} — each rung needs its own section"
    )
    assert [title.split(".")[0] for _, title in ladder] == [
        str(number) for number, _ in LADDER_STEPS
    ], (
        "the ladder sections of the Chinese page are not in order: "
        + ", ".join(title for _, title in ladder)
    )


def test_locale_pairing_complete() -> None:
    """The two pages exist, and the Chinese one is not half a page."""
    assert _EN_DOC.is_file(), (
        f"{_EN_DOC.relative_to(_REPO_ROOT)} does not exist — the English page "
        "is the one this page mirrors"
    )
    assert _ZH_DOC.is_file(), (
        f"{_ZH_DOC.relative_to(_REPO_ROOT)} does not exist — the pairing is "
        "incomplete, so a Chinese reader silently gets English"
    )

    en_sections = _sections(_EN_DOC.read_text(encoding="utf-8"))
    zh_sections = _sections(_zh_text())

    for level, label in ((2, "top-level"), (3, "third-level")):
        en_count = sum(1 for lvl, _, _ in en_sections if lvl == level)
        zh_count = sum(1 for lvl, _, _ in zh_sections if lvl == level)
        assert zh_count == en_count, (
            f"the Chinese page has {zh_count} {label} sections, the English "
            f"page has {en_count} — the two have to mirror each other, "
            "otherwise the Chinese page is a fragment of the English one"
        )
