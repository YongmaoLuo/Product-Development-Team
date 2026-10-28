"""The orphan reaper's English page must say what the reaper may kill.

Why this gate exists
--------------------
The reaper is the only component in this system that sends signals to
processes it did not start in the current round.  Its safety does not
come from the code being careful — it comes from a set of rules that
are only useful if an operator can *read* them: which variables turn it
off, which marker makes a process "ours", where the audit trail lands,
and, above all, **under which conditions it refuses to kill**.

A page that documents the knobs but not the boundary leaves an
operator with a way to make the reaper more aggressive and no way to
learn what "aggressive" costs.  So the page has to answer those
questions in writing, and this file pins that it does.

What is pinned
--------------
1. **The page exists** and is reachable from the site's navigation.
2. **It names the interface**: the five configuration variables, the
   three spawn markers, the kill-event log, and the run modes.
3. **It states the false-kill boundary**: the four conditions under
   which the reaper must leave a process alone.
4. **It explains the macOS unreadable-environment case**, because an
   operator reading the log has to be able to tell a designed
   behaviour apart from a fault.

Scope, stated honestly: this checks that the page *mentions* the right
names.  Every name checked here is a name the code actually uses
(``backend/orphan_reaper.py``), so a page that mentions them cannot
have drifted away from a renamed constant without failing here.  It
cannot check that the prose around them is *correct* — only a reader
can do that.

The Chinese sibling of this page is a separate piece of work: the
bilingual gate (``backend/tests/static_gates/test_docs_are_bilingual.py``)
checks that a ``.zh.md`` file exists, not that it says anything, so it
is not a substitute for the checks below.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The repository root — three levels up from ``backend/tests/unit``.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The English page.
_DOC = _REPO_ROOT / "docs" / "operations" / "orphan-reaper.md"

#: The site configuration the page has to be listed in.
_MKDOCS_YML = _REPO_ROOT / "mkdocs.yml"

#: The five configuration variables an operator may need to set.
#:
#: Their names, defaults and meanings are the ones
#: ``backend/orphan_reaper.py`` resolves them from; the page has to
#: carry the same names, spelled the same way, because a variable name
#: is what an operator types.
REQUIRED_ENV_VARS = (
    "PDT_ORPHAN_REAPER",
    "PDT_ORPHAN_REAPER_INTERVAL",
    "PDT_ORPHAN_REAPER_PROTECT_PORTS",
    "PDT_ORPHAN_REAPER_GRACE",
    "PDT_ORPHAN_REAPER_IDLE_EXIT",
)

#: The three markers that make a process recognisable as PDT's.
REQUIRED_MARKERS = (
    "PDT_SPAWN_OWNER_PID",
    "PDT_SPAWN_OWNER_START",
    "PDT_SPAWN_KIND",
)

#: The audit trail the reaper appends to.
REQUIRED_LOG = "kill_events.jsonl"

#: The run modes an operator has to be able to choose between.
REQUIRED_FLAGS = ("--once", "--daemon", "--dry-run")

#: The four conditions under which the reaper must not kill a process.
#:
#: Each entry is ``(label, required fragments)``.  The fragments are
#: the reason code the code records for that case — the same string an
#: operator will find in ``kill_events.jsonl`` — plus the wording the
#: page has to use for the case.  A page that lists a reason code in a
#: table of possible outcomes without saying what it means does not
#: pass: the point of the page is that the boundary is *stated*, not
#: merely mentioned.
FALSE_KILL_BOUNDARIES = (
    (
        "a process without a PDT marker is never a candidate",
        ("unmarked", "never a candidate"),
    ),
    (
        "a process whose owner is alive is never a candidate",
        ("owner_alive", "never a candidate"),
    ),
    (
        "a process holding a protected port is never a candidate",
        ("protected_port", "never a candidate"),
    ),
    (
        "a process whose environment cannot be read is skipped",
        ("env_unreadable", "fail-closed"),
    ),
)

#: The six rungs of the verdict ladder, in the order they are walked.
#:
#: The numbering matters: the Chinese sibling of this page mirrors it,
#: so the two cannot drift apart silently.
LADDER_STEPS = (
    (1, "No PDT marker"),
    (2, "Owner still alive"),
    (3, "PID reuse"),
    (4, "Protected port"),
    (5, "The reaper itself"),
    (6, "Owner is dead"),
)


def _doc_text() -> str:
    """Return the English page's text."""
    return _DOC.read_text(encoding="utf-8")


def _nav_operations_block() -> str:
    """Return the lines of the ``Operations`` section of the nav.

    Parsed out of the file as text rather than through a YAML loader:
    the check is about *where* the entry sits in the navigation, and
    the file's own indentation says that without a second dependency.
    """
    lines = _MKDOCS_YML.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "- Operations:":
            start = index
            break
    assert start is not None, (
        "mkdocs.yml no longer has an `- Operations:` section in its nav — "
        "the orphan reaper page would be unreachable from the site"
    )

    block = []
    for line in lines[start + 1:]:
        # A sibling of `Operations` (two spaces, then the dash) ends the
        # section; its own entries are indented deeper.
        if line.startswith("  - "):
            break
        block.append(line)
    return "\n".join(block)


def test_english_doc_exists() -> None:
    """The page is where an operator is told how to run the reaper."""
    assert _DOC.is_file(), (
        f"{_DOC.relative_to(_REPO_ROOT)} does not exist — the reaper ships "
        "without any page saying how to run it or what it may kill"
    )


def test_english_doc_covers_env_and_markers() -> None:
    """Every name an operator types or greps for has to be on the page."""
    text = _doc_text()

    missing = [name for name in REQUIRED_ENV_VARS if name not in text]
    assert not missing, (
        "the orphan reaper page does not mention these configuration "
        "variables, so an operator cannot find out how to configure it: "
        + ", ".join(missing)
    )

    missing = [name for name in REQUIRED_MARKERS if name not in text]
    assert not missing, (
        "the orphan reaper page does not mention these spawn markers, so "
        "the page cannot explain how a process is recognised as ours: "
        + ", ".join(missing)
    )

    assert REQUIRED_LOG in text, (
        f"the orphan reaper page does not mention `{REQUIRED_LOG}`, so an "
        "operator cannot find the audit trail of what the reaper did"
    )

    missing = [flag for flag in REQUIRED_FLAGS if flag not in text]
    assert not missing, (
        "the orphan reaper page does not mention these run modes: "
        + ", ".join(missing)
    )


def test_english_doc_states_false_kill_boundary() -> None:
    """The four "never kill" conditions have to be written down."""
    text = _doc_text()

    for label, fragments in FALSE_KILL_BOUNDARIES:
        missing = [fragment for fragment in fragments if fragment not in text]
        assert not missing, (
            f"the orphan reaper page does not state the boundary for "
            f"{label}: " + ", ".join(missing)
        )


def test_english_doc_lists_the_verdict_ladder() -> None:
    """The six rungs are numbered, so the Chinese page can mirror them."""
    text = _doc_text()

    for number, title in LADDER_STEPS:
        assert re.search(
            r"^###\s*%d\.\s*%s" % (number, re.escape(title)),
            text,
            re.MULTILINE,
        ), (
            f"the orphan reaper page has no `### {number}. {title}` section — "
            "the verdict ladder has to be numbered, in order, so that the "
            "Chinese sibling of this page can mirror it"
        )


def test_english_doc_explains_the_unreadable_environment_on_macos() -> None:
    """An unreadable environment on macOS is by design, not a fault.

    On macOS the environment is read through ``ps -E``, and Apple's own
    binaries are protected by SIP: their environment cannot be read at
    all.  The reaper records those as ``env_unreadable`` and skips
    them.  An operator who meets that reason code in the log has to be
    able to tell it apart from a malfunction.
    """
    text = _doc_text()

    for fragment in ("macOS", "Apple", "SIP", "env_unreadable"):
        assert fragment in text, (
            f"the orphan reaper page does not mention `{fragment}`, so an "
            "operator meeting that case in the log cannot tell what it means"
        )

    assert re.search(r"by design", text, re.IGNORECASE), (
        "the orphan reaper page does not say that the macOS unreadable-"
        "environment case is by design — an operator has to be able to "
        "tell a designed behaviour apart from a fault"
    )


def test_mkdocs_nav_lists_page() -> None:
    """The page has to be reachable from the site's navigation."""
    block = _nav_operations_block()

    assert "operations/orphan-reaper.md" in block, (
        "the `Operations` section of the nav in mkdocs.yml does not list "
        "`operations/orphan-reaper.md` — the page exists but nobody can "
        "find it:\n" + block
    )
