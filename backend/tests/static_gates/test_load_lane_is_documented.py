"""Static gate: the CPU load lane is documented, and only in the load lane.

Why this gate exists
--------------------
Task 1 gave this repository one sanctioned way to make the host busy:
``backend/tests/_load_rig.py::cpu_load`` -- real child processes, each
owning a counter slot, every one of them reclaimed when the block ends.
Task 2 gave it one shared gate (``backend/tests/_host_load.py``) that asks
the host whether it is idle *before* a worker is started.  Task 3 forbade
the hand-rolled busy loop.

What none of those three said is **where a load case belongs**.  The rig
makes the host busy on purpose, so it is a phase 3 / nightly affair and
never a fast-lane affair: a load case running beside the parallel unit
lane does not measure the code under test, it measures the machine.  That
sentence lived only in the head of whoever wrote the rig, so the next
reader has to guess -- and the guess they are likely to make is "wherever
the test happens to live".

A rule that is only written down is a rule that is only followed by
whoever happens to read the document, so the document is pinned here.

What this gate pins
-------------------
1. the English testing guide names the rig and says which phase owns it;
2. the gate script's "Shape of the run" prose names the rig, and the
   phase 3 marker expression stays the one it is today -- renaming it has
   to fail here rather than silently move a load case into another lane;
3. the Chinese guide names the rig too: the bilingual gate checks that a
   translation *exists*, not that it says anything;
4. both guides say the load never runs in the fast lane.

Scope, stated honestly: this gate reads two documents and one script.  It
says nothing about whether a load case is correctly *marked* -- that is
the phase partition's job -- and nothing about whether the rig behaves.
It says the rule is written down where a reader will find it.
"""

from __future__ import annotations

from pathlib import Path

#: The repository root -- this file lives at ``backend/tests/static_gates/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The gate script whose phase 3 owns the load cases.
GATE_SCRIPT = REPO_ROOT / "scripts" / "run_tests.sh"

#: The English testing guide.
TESTING_GUIDE = REPO_ROOT / "docs" / "development" / "testing.md"

#: The Chinese testing guide.
TESTING_GUIDE_ZH = REPO_ROOT / "docs" / "development" / "testing.zh.md"

#: The heading the English guide's load section carries.
LOAD_SECTION = "## CPU load tests"

#: The heading the Chinese guide's load section carries.
LOAD_SECTION_ZH = "## CPU 负载测试"

#: The phase 3 marker expression, exactly as the gate script declares it.
PHASE3_DECLARATION = 'PHASE3_ARGS=(-m "time_sensitive or perf"'

#: The two anchors of the "Shape of the run" prose in the gate script.
#: Neither anchor is part of the block, so the extraction survives an edit
#: anywhere else in the script.
_SHAPE_BLOCK_START = "# Shape of the run"
_SHAPE_BLOCK_END = "# Marker expressions and per-phase budgets"

#: The rig this repository sanctions for making the host busy.
_LOAD_RIG = "_load_rig"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lines(path: Path) -> list[str]:
    return _read(path).splitlines()


def _shape_of_the_run_block() -> str:
    """The prose of the gate script that describes the shape of the run.

    The block is delimited by two anchors, so the extraction does not depend
    on the script's line numbering -- which drifts with every edit.
    """
    text = _read(GATE_SCRIPT)
    start = text.index(_SHAPE_BLOCK_START)
    end = text.index(_SHAPE_BLOCK_END)
    assert start < end, (
        "the 'Shape of the run' prose no longer precedes the marker "
        "expressions in scripts/run_tests.sh -- update this gate's anchors "
        "rather than letting it read the wrong block"
    )
    return text[start:end]


def test_the_testing_guide_names_the_load_rig() -> None:
    """The English guide must say which lane owns a load case."""
    text = _read(TESTING_GUIDE)

    assert LOAD_SECTION in text, (
        f"docs/development/testing.md has no '{LOAD_SECTION}' section. Add "
        "it after '## The lanes': a case that needs the host to be busy uses "
        "tests/_load_rig.py::cpu_load, and that belongs in phase 3 / "
        "nightly -- not in the fast lane."
    )
    assert _LOAD_RIG in text, (
        "docs/development/testing.md never names _load_rig. The guide has to "
        "point at the one sanctioned way to make the host busy, or the next "
        "reader writes their own busy loop."
    )
    assert "phase 3" in text.lower(), (
        "docs/development/testing.md does not say which phase owns the load "
        "cases. The rig makes the host busy on purpose, so it belongs in "
        "phase 3 / nightly."
    )


def test_the_gate_script_assigns_load_to_phase_3() -> None:
    """The gate script's phase 3 prose must name the rig.

    The marker expression is pinned as well: it is what puts the load
    cases in phase 3, so renaming it has to break this gate rather than
    move the load cases somewhere else without anybody noticing.
    """
    text = _read(GATE_SCRIPT)

    assert PHASE3_DECLARATION in text, (
        "scripts/run_tests.sh no longer declares "
        'PHASE3_ARGS=(-m "time_sensitive or perf" ...). If the phase 3 '
        "marker expression changed, the load cases moved with it -- update "
        "this gate deliberately rather than by accident."
    )

    block = _shape_of_the_run_block()
    assert _LOAD_RIG in block, (
        "the 'Shape of the run' prose in scripts/run_tests.sh does not "
        "mention _load_rig. The phase 3 prose has to say that the load "
        "cases run here -- and only here."
    )


def test_the_chinese_guide_names_the_load_rig() -> None:
    """The Chinese guide has to carry the rule too.

    The bilingual gate checks that a translation *exists*; it does not read
    it. A Chinese reader therefore gets the English fallback for a rule
    that is exactly the kind of rule a reader needs in their own language.
    """
    text = _read(TESTING_GUIDE_ZH)

    assert LOAD_SECTION_ZH in text, (
        f"docs/development/testing.zh.md has no '{LOAD_SECTION_ZH}' section. "
        "Add it after '## 各条 lane', mirroring the English section."
    )
    assert _LOAD_RIG in text, (
        "docs/development/testing.zh.md never names _load_rig. The Chinese "
        "guide has to point at the one sanctioned way to make the host "
        "busy, or the next reader writes their own busy loop."
    )


def test_the_guide_forbids_load_in_the_fast_lane() -> None:
    """Both guides must forbid the load in the fast lane, in one line each.

    The check is per line on purpose: a rule split across two lines of
    prose is easy to lose in a re-wrap, and the sentence that matters is
    the one that carries both halves -- the rig, and the lane it may not
    run in.
    """
    english = [
        line
        for line in _lines(TESTING_GUIDE)
        if _LOAD_RIG in line and "never" in line
    ]
    assert english, (
        "no line of docs/development/testing.md carries both '_load_rig' and "
        "'never'. The guide has to state that the load never runs in the "
        "fast lane: a load running beside the parallel lane measures the "
        "machine, not the code under test."
    )

    chinese = [
        line
        for line in _lines(TESTING_GUIDE_ZH)
        if _LOAD_RIG in line and "fast lane" in line
    ]
    assert chinese, (
        "no line of docs/development/testing.zh.md carries both '_load_rig' "
        "and 'fast lane'. The Chinese guide has to carry the same rule as "
        "the English one."
    )
