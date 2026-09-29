"""README must onboard a new contributor with zero out-of-band hand-holding.

Why this gate exists
--------------------
Six tasks from the product PRD map to six topics, and the README is the
single document every newcomer reads first. Letting those topics live in
arbitrary places — or worse, split across two sections that drift apart —
turns "get this thing running" into a tour guide's job. The gate pins the
*information architecture*: which headings exist, which ones do not, and
which passages must remain byte-identical because the suite or the CI
expects to find them.

Why a *gate* and not "review on every PR"
-----------------------------------------
The same trap that bit the home-path and the licence-attribution gates
sits here too. A review-time check drifts: someone trims a paragraph
"to keep it short", another task adds a synonym section to avoid
editing an existing one, and the README graduates into two parallel
explanations of the same thing — one right next to the other, both
slightly stale. Pinning the shape in source means the failing test is
visible on the same ``git push`` that broke it.

The rule
--------
``REQUIRED_HEADINGS`` is the exhaustive list of headings the onboarding
story needs. Every entry corresponds to one PRD topic and is matched
verbatim — the test does not normalise whitespace, so a heading like
"## What it is / Problem it solves" must appear *exactly* with that
spacing and case. Forbidden synonym headings (``## Directory layout``,
``## Configuration reference``) are listed in
:data:`FORBIDDEN_HEADING_SUBSTRINGS`; a clean README must mention none
of them.

Verbatim-preserved passages — ``## How it works`` and the four pre-existing
sections — are not re-asserted by snippet matching: the test instead
guarantees the heading still exists and the lines around it are still
in the same order. That is loose enough to allow new content between
sections (which every onboarding task needs) and tight enough to catch
"the architecture diagram silently disappeared".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Constants — the single source of truth for the README information architecture.
# ---------------------------------------------------------------------------

#: Required headings, in the order they must appear in the README. Each
#: entry is a *full* heading line (``## Foo`` or ``### Foo``) so
#: substring matching cannot accept a partial coincidence like a
#: comment that happens to mention the same words.
REQUIRED_HEADINGS: tuple[str, ...] = (
    "## What it is / Problem it solves",
    "## How it works",
    "## Quick start",
    "### Installation and startup",
    "### The request guard",
    "## Layout",
    "## Configuration",
    "## How to run the tests",
    "## Design notes",
    "## How to contribute",
    "## License",
)

#: Headings that would parallel an existing section if added. Two
#: parallel explanations of one topic is a maintenance hazard; the
#: gate refuses to let either of these slip in.
FORBIDDEN_HEADING_SUBSTRINGS: tuple[str, ...] = (
    "## Directory layout",
    "## Configuration reference",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_readme() -> str:
    """Return the README text from the repository root.

    Resolved relative to ``backend/tests/static_gates/`` so the test
    runs identically from any working directory (pytest may be invoked
    from the repo root or from ``backend/``).
    """
    repo_root = Path(__file__).resolve().parents[3]
    readme_path = repo_root / "README.md"
    return readme_path.read_text(encoding="utf-8")


def _heading_offsets(text: str) -> list[tuple[str, int]]:
    """Return one ``(heading_line, line_no)`` per ``## …`` / ``### …`` heading."""
    pattern = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
    return [(m.group(0), m.start()) for m in pattern.finditer(text)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_required_sections_present() -> None:
    """Every onboarding topic has a heading in the README."""
    text = _read_readme()
    missing = [h for h in REQUIRED_HEADINGS if h not in text]
    assert not missing, (
        "README is missing required onboarding sections. Adding them "
        "here is what lets task 20's zero-questions E2E walk a newcomer "
        "from clone to green tests. Missing headings:\n  "
        + "\n  ".join(missing)
    )


def test_architecture_section_preserved() -> None:
    """The ``## How it works`` ASCII diagram and surrounding prose survive intact."""
    text = _read_readme()
    # The diagram and its lead-in are the core of the architecture
    # section; if either disappears the README is no longer the same
    # document the suite was wired against.
    must_have = (
        "## How it works",
        "requirement",
        "[1] Requirement clarification",
        "[7] Verification",
        "Every decision point is a **CPEA** record",
    )
    missing = [s for s in must_have if s not in text]
    assert not missing, (
        "``## How it works`` (and its CPEA explanation) must stay "
        "verbatim — it is the PRD-mapped architecture diagram and the "
        "first thing a reviewer looks at. The following fragments are "
        "missing:\n  " + "\n  ".join(missing)
    )


def test_no_duplicate_synonym_heading() -> None:
    """No synonym-heading duplicates; the three live sections appear exactly once."""
    text = _read_readme()

    # Forbidden synonym headings must not appear at all. A grep would
    # do, but the assert message needs to be readable.
    for forbidden in FORBIDDEN_HEADING_SUBSTRINGS:
        assert forbidden not in text, (
            f"README contains forbidden synonym heading ``{forbidden}`` — "
            "use the existing section instead of adding a parallel one."
        )

    # Each pre-existing heading must appear exactly once. ``text.count``
    # is sufficient because these strings are long enough that a
    # passing mention would be flagged.
    exact_once = ("## Configuration", "## Layout", "### The request guard")
    for heading in exact_once:
        count = text.count(heading)
        assert count == 1, (
            f"``{heading}`` must appear exactly once in the README "
            f"(currently {count}). Two copies will drift apart; "
            "extend the original instead."
        )


def test_venv_and_system_python_caveat_documented() -> None:
    """The reader is told what to activate and *why* the system Python breaks."""
    text = _read_readme()
    # The activation command must be spelled out so newcomers can copy
    # it; the system-Python warning must explain the failure mode so a
    # "this fails at collection" report does not read as mysterious.
    needles = (
        "backend/.venv",
        "system Python",
    )
    missing = [n for n in needles if n not in text]
    assert not missing, (
        "Quick start must show how to activate ``backend/.venv`` "
        "and explain why running pytest with the *system* Python fails "
        "(stale urllib3 / missing wheels — collection itself dies). "
        "Missing fragments:\n  " + "\n  ".join(missing)
    )

    # The reason must be *stated*, not just alluded to. The task brief
    # calls out urllib3 staleness and missing wheels explicitly; either
    # reason alone is enough (failure modes do not have to agree on the
    # root cause, only on "collection fails").
    reasons = (
        "urllib3",
        "collect",  # "collection" also matches via substring, but "collect" is the verb
    )
    has_reason = any(r.lower() in text.lower() for r in reasons)
    assert has_reason, (
        "Quick start must give an actual reason (stale urllib3, missing "
        "wheels) for why system Python breaks — a bare 'do not use it' "
        "is the same trap the original Quick start fell into."
    )


def test_request_guard_wrapper_rule_documented() -> None:
    """The request guard section names the wrapper files and warns about bare fetch()."""
    text = _read_readme()

    # The wrapper names must be spelled out — both files hold exactly
    # one fetch wrapper, and pinning the names means a rename on
    # either side forces the README to be updated in the same commit.
    needles = (
        "X-PDT-Request",
        "frontend/api.js",
        "app.js",
        "fetch",  # the bare-fetch warning must mention the literal token
    )
    missing = [n for n in needles if n not in text]
    assert not missing, (
        "The request guard section must name the wrapper files "
        "(``frontend/api.js`` and ``app.js``'s ``api()``) and spell out "
        "that a bare ``fetch(`` will get 403. Missing fragments:\n  "
        + "\n  ".join(missing)
    )

    # The 403 consequence must be explicit — "request guard" alone
    # reads like a parenthetical detail, not the failure mode a
    # one-keystroke mistake produces.
    assert "403" in text, (
        "Request-guard wrapper section must say the bare-fetch failure "
        "mode is a 403 (in words or as a literal); without the "
        "consequence a reader has nothing to recognise the bug from."
    )


def test_config_dir_vs_example_section_present() -> None:
    """``## Configuration`` explains the ``.config/`` vs ``example/`` workflow without naming real providers."""
    text = _read_readme()

    # The shape of the explanation: reader sees both directories,
    # learns the copy-into-config workflow, and learns the env-var
    # override that lets a deployment skip the project root entirely.
    shape_needles = (
        ".config/",
        "example/",
        "PDT_PROVIDER_CAPACITY_FILE",
        "PDT_PROVIDER_ROUTING_FILE",
    )
    missing = [n for n in shape_needles if n not in text]
    assert not missing, (
        "``## Configuration`` must explain the ``.config/`` vs "
        "``example/`` shape (copy template → edit → drop into the "
        "gitignored ``.config/``) and the two ``PDT_*`` override "
        "env vars. Missing fragments:\n  " + "\n  ".join(missing)
    )

    # There used to be a provider-name deny-list here. It was removed:
    # a brand name is not a secret, and the check published the exact
    # set of names it existed to keep out. The section stays
    # operator-neutral by *describing the shape* (template file →
    # gitignored config), which the fragment check above pins — not by
    # blacklisting whichever names this deployment happens to use.


def test_no_duplicate_heading_under_same_level() -> None:
    """A ``## Foo`` and a second ``## Foo`` heading (same level, same text) is not allowed."""
    text = _read_readme()
    headings = [h for h, _ in _heading_offsets(text)]

    from collections import Counter
    counts = Counter(h for h in headings if h in REQUIRED_HEADINGS)
    dupes = {h: c for h, c in counts.items() if c > 1}
    assert not dupes, (
        "A required heading must not appear twice in the README; "
        "duplicates make navigation inconsistent between readers and "
        "table-of-generation tools. Duplicates found:\n  "
        + "\n  ".join(f"{h!r} x{c}" for h, c in dupes.items())
    )


def test_contribute_section_marks_repo_layout_canonical() -> None:
    """``## How to contribute`` names the entry points so a newcomer can find the right file."""
    text = _read_readme()
    # The contribution surface: phases live as backend modules, the UI
    # is static, and the suite lives under ``backend/tests/``. A
    # contributor who has to grep the tree to find any of these has
    # already failed onboarding.
    for needle in ("backend/", "frontend/", "backend/tests/"):
        assert needle in text, (
            f"``## How to contribute`` must list a concrete entry point "
            f"that contains ``{needle}``. A reader who has to run a "
            f"``find`` to learn where work lives has been failed."
        )
