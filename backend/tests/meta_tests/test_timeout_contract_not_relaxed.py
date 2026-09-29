"""Meta-test pinning the timeout contract and the three-layer performance baseline.

Background
----------
The "pytest 全绿" badge is meaningless unless we can distinguish a
genuinely-green run from a silently-relaxed one.  Two historical
incidents made the second shape concrete:

  * 2026-09: a hung pytest pinned its SQLite WAL writer and grew its
    stderr-capture file to 30+ GB before the supervisor noticed.  The
    fix landed in ``backend/pytest.ini`` as ``timeout = 300`` with
    ``timeout_method = thread`` (the ``signal`` method is broken under
    threading on macOS).  A contributor who edits the file to widen or
    delete the cap — even with good intent, "the new suite is too slow
    for 5 minutes" — silently re-opens the hang path, because the
    suite passes "all green" while the next hung run still hangs.

  * An earlier sweep removed the ``perf`` and ``e2e`` markers from the
    registry while still shipping tests that referenced them;
    ``--strict-markers`` then aborted collection for the affected
    modules under the next run.  Both markers must stay registered
    even when their test counts drift to zero — the marker is what
    keeps the layer partitionable from the PR lane.

This module pins four contracts so neither regression returns under
a different shape:

  1. ``test_pytest_timeout_is_not_relaxed`` — ``timeout = 300`` still
     present in ``backend/pytest.ini`` and not widened.
  2. ``test_timeout_method_is_thread`` — ``timeout_method = thread``
     still present (the signal-based method does not work on macOS).
  3. ``test_e2e_and_perf_markers_survive`` — the ``e2e`` and ``perf``
     markers still appear in ``[pytest] markers = ...`` so a later
     sweep cannot drop them without tripping the gate.
  4. ``test_perf_record_section_has_three_layers`` — the audit's
     **sweep record** carries a ``## 性能记录`` section that lists each
     of unit / integration / e2e with both a case count and a
     wall-clock measurement, so a 20%+ drift can be traced back to a
     specific layer in a PR review.  That record is operator-local
     (``.config/``, gitignored) because it is a measurement of one
     machine's build of the suite — see the note on ``_SWEEP_MD``
     below and the rule in ``SECURITY_AUDIT.md``'s entry template.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
#
# The audit doc owns the "where is the config file" knowledge and pins it
# via ``Path(__file__).resolve().parents[N]`` rather than an env var: a
# contract that depends on a developer's environment is one that quietly
# breaks the moment someone clones the repo to a different layout.

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/
_PYTEST_INI = _BACKEND_DIR / "pytest.ini"

_REPO_ROOT = _BACKEND_DIR.parent  # repo root
_AUDIT_MD = _REPO_ROOT / "SECURITY_AUDIT.md"

#: The audit's operator-local **sweep record** — the process half of the
#: audit, split out of ``SECURITY_AUDIT.md`` on 2026-09-28 because it
#: describes what the sweep did rather than what is wrong with the code:
#: a dated wall-clock baseline, the negative-result coverage map, and the
#: grep workpaper table.
#:
#: It lives under the gitignored ``.config/``, so it is absent on a fresh
#: clone and the test reading it **skips** rather than fails.  That is the
#: intended trade: the baseline is a claim about one machine's build of
#: the suite, which a stranger cannot verify and does not need.  The gate
#: still binds on the machine that owns the record, which is the only
#: machine the drift-attribution contract is for.
_SWEEP_MD = _REPO_ROOT / ".config" / "security-audit" / "sweep.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_pytest_ini() -> configparser.ConfigParser:
    """Return the parsed ``backend/pytest.ini``.

    ``ConfigParser`` is used rather than a hand-rolled regex because
    the marker block is already a structured ``name: value`` shape —
    a regex would silently drift from the canonical parser the
    upstream tools (pytest, tox) use, and a future reformat would
    break the test in ways the operator could not reproduce locally.
    """
    assert _PYTEST_INI.exists(), (
        f"{_PYTEST_INI} is missing; the timeout contract cannot be "
        f"evaluated against an absent config file"
    )
    parser = configparser.ConfigParser()
    # ``configparser`` lower-cases keys by default; pytest treats
    # ``[pytest]`` literally, so preserve case to keep diffs readable.
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(_PYTEST_INI, encoding="utf-8")
    assert parser.has_section("pytest"), (
        f"{_PYTEST_INI} is missing the `[pytest]` section; pytest will "
        f"refuse to load it and the entire suite silently collects zero"
    )
    return parser


def _registered_markers(parser: configparser.ConfigParser) -> set[str]:
    """Return the set of marker names declared under ``[pytest] markers =``.

    The block is a multi-line value with one marker per line, each in
    the shape ``<name>: <description>``.  ``configparser`` already
    splits on newlines; the only parsing left is to drop the trailing
    ``: <description>`` so we can compare names directly.
    """
    raw = parser.get("pytest", "markers", fallback="")
    names: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip optional inline comments — pytest's parser ignores
        # text after ``#`` in a marker line.
        marker_part = stripped.split("#", 1)[0].strip()
        if ":" not in marker_part:
            continue
        names.add(marker_part.split(":", 1)[0].strip())
    return names


# ---------------------------------------------------------------------------
# TDD spec 1 — ``timeout = 300`` is still present
# ---------------------------------------------------------------------------


def test_pytest_timeout_is_not_relaxed() -> None:
    """``timeout`` in ``backend/pytest.ini`` must equal ``300``.

    A contributor who widens the cap to "the new suite is too slow
    for 5 minutes" silently re-opens the 2026-09 hang path; a
    contributor who deletes the cap entirely lets the next hung run
    grow its stderr-capture file without bound.  Lowering the cap is
    allowed (and we explicitly do not test against that direction)
    because a tighter cap cannot make a hang worse — it can only
    fail-louder, sooner.
    """
    parser = _load_pytest_ini()
    assert parser.has_option("pytest", "timeout"), (
        f"{_PYTEST_INI} no longer declares `timeout = ...`; "
        f"a hung pytest will grow its stderr-capture file without bound "
        f"(2026-09 incident: 30+ GB before the supervisor noticed). "
        f"Restore `timeout = 300`."
    )
    value = parser.get("pytest", "timeout").strip()
    assert value == "300", (
        f"backend/pytest.ini `timeout = {value}`; "
        f"the contract requires `timeout = 300` and the timeout "
        f"MUST NOT be widened without a written waiver in "
        f"SECURITY_AUDIT.md explaining the new ceiling. "
        f"Lowering the cap (e.g. to 240) is allowed but must be "
        f"documented in the same audit entry."
    )


# ---------------------------------------------------------------------------
# TDD spec 2 — ``timeout_method = thread`` is still present
# ---------------------------------------------------------------------------


def test_timeout_method_is_thread() -> None:
    """``timeout_method`` in ``backend/pytest.ini`` must equal ``thread``.

    The ``signal`` method is broken under threading on macOS (SIGALRM
    is not delivered to threads other than the main thread); pytest-
    timeout's docs call out that it falls back automatically.  Pinning
    the value here turns a silent fallback into an explicit contract
    that a contributor must consciously change.
    """
    parser = _load_pytest_ini()
    assert parser.has_option("pytest", "timeout_method"), (
        f"{_PYTEST_INI} no longer declares `timeout_method = ...`; "
        f"pytest-timeout will pick a method automatically and the "
        f"choice can drift between runners. Restore "
        f"`timeout_method = thread` (macOS-safe; pytest-timeout also "
        f"documents that signal-based timeouts do not work under "
        f"threading)."
    )
    value = parser.get("pytest", "timeout_method").strip()
    assert value == "thread", (
        f"backend/pytest.ini `timeout_method = {value}`; "
        f"the contract requires `timeout_method = thread` so the "
        f"hang-reaper works on every platform in CI."
    )


# ---------------------------------------------------------------------------
# TDD spec 3 — ``e2e`` and ``perf`` markers still survive
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registered_markers() -> set[str]:
    parser = _load_pytest_ini()
    return _registered_markers(parser)


def test_e2e_and_perf_markers_survive(registered_markers: set[str]) -> None:
    """The ``e2e`` and ``perf`` markers must still be declared.

    Both markers are required for the three-layer performance baseline
    in ``SECURITY_AUDIT.md``'s ``## 性能记录`` section: ``-m e2e``
    isolates the slow-layer tests from the PR lane and ``-m perf``
    isolates the benchmarks.  ``--strict-markers`` aborts collection
    for a test that uses an unregistered marker, so a silent deletion
    cascades into "0 tests collected" with no obvious cause.
    """
    missing = [m for m in ("e2e", "perf") if m not in registered_markers]
    assert not missing, (
        f"{_PYTEST_INI} no longer declares the {missing!r} marker(s); "
        f"`--strict-markers` will abort collection for any test that "
        f"uses them and the three-layer performance baseline in "
        f"SECURITY_AUDIT.md will collapse to two layers. "
        f"Restore the missing entries under `[pytest] markers = ...` "
        f"(see the same block for the canonical description text)."
    )


# ---------------------------------------------------------------------------
# TDD spec 4 — `## 性能记录` section has three layers with metrics
# ---------------------------------------------------------------------------


_PERF_RECORD_HEADING_RE = re.compile(
    r"^#{1,6}\s+性能记录\s*$", flags=re.MULTILINE
)


def _sweep_text() -> str:
    """Return the full text of the operator-local sweep record.

    Skips when the record is absent — a fresh clone has no ``.config/``,
    and skipping there is honest: the baseline is a measurement of this
    machine's build, so a stranger has nothing to check it against.  The
    record's *shape* is still pinned on the machine that owns it.
    """
    if not _SWEEP_MD.exists():
        pytest.skip(
            f"{_SWEEP_MD} is absent — the audit sweep record is "
            f"operator-local (gitignored), so this machine has no "
            f"performance baseline to pin"
        )
    return _SWEEP_MD.read_text(encoding="utf-8")


def _perf_record_section(text: str) -> str:
    """Return the body of the ``## 性能记录`` section.

    The body runs from the heading until the next heading at the
    same or SHALLOWER level (matching how Markdown sections nest — a
    ``###`` sub-heading is part of the section; a ``##`` at the same
    level closes it).  An absent section surfaces here as an empty
    string — the caller fails the assertion rather than letting the
    empty section pass vacuously.
    """
    match = _PERF_RECORD_HEADING_RE.search(text)
    assert match is not None, (
        f"{_SWEEP_MD} is missing a `## 性能记录` section; "
        "the three-layer performance baseline is what makes a "
        "'pytest 全绿' badge falsifiable when the wall-clock drifts."
    )
    # Recover the heading depth from the matched line so the section
    # boundary is computed at the SAME level (not at every ``###``
    # sub-heading, which would truncate the body before the layer
    # blocks).
    heading_line_start = text.rfind("\n", 0, match.start()) + 1
    heading_line = text[heading_line_start : match.end()]
    heading_level = len(heading_line) - len(heading_line.lstrip("#"))
    assert heading_level >= 1, (
        f"matched performance-record heading is not a Markdown "
        f"heading: {heading_line!r}"
    )
    start = match.end()
    # A sub-section (``###``) belongs to this section; the next
    # sibling (``##``) or shallower (``#``) heading closes it.
    # Inverted from the ``#+`` regex above: the quantifier upper
    # bound is the heading depth, not the depth itself.
    boundary_re = re.compile(
        r"^#{1," + str(heading_level) + r"}\s+",
        flags=re.MULTILINE,
    )
    next_heading = boundary_re.search(text, pos=start)
    end = next_heading.start() if next_heading else len(text)
    return text[start:end]


# A layer heading inside the performance record section — a line that
# announces one of unit / integration / e2e.  We deliberately accept
# the layer name with or without backticks and with a trailing colon
# because both shapes read fine in a Markdown diff.
_LAYER_HEADING_RE = re.compile(
    r"^#{2,6}\s+(?:`)?(?P<layer>unit|integration|e2e)(?:`)?\b",
    flags=re.MULTILINE | re.IGNORECASE,
)


# Phrases that count as "this layer carries a case count" — the
# literal Chinese phrase (用例数) and the English fallback (cases).
# Both are accepted so a future English refactor of the section
# does not silently invalidate the gate.
_CASE_COUNT_RE = re.compile(r"用例数|case\s*count|\bN\s*=\s*\d+", flags=re.IGNORECASE)

# Wall-clock phrases — the literal phrase (墙钟 / 墙钟时间) and
# English fallbacks (wall-?clock / duration / elapsed).  We also
# accept a unit suffix that pytest emits (``s``, ``sec``, ``seconds``,
# ``min``) so a "0.42s" or "12.5s" reading qualifies without forcing
# the author to spell the Chinese phrase.
_WALLCLOCK_RE = re.compile(
    r"墙钟|wall[\s_-]?clock|duration|elapsed|"
    r"\b\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds|min)\b",
    flags=re.IGNORECASE,
)


def test_perf_record_section_has_three_layers() -> None:
    """``## 性能记录`` must list unit / integration / e2e with case count and wall-clock.

    Three-layer coverage is what lets a future PR argue "this
    security patch added 200ms to the unit layer" instead of just
    "the suite is 20% slower".  A two-layer section is too coarse to
    attribute the drift; a four-layer section (e.g. unit/integration/
    e2e/perf) is fine, but at minimum these three layers must each
    carry both metrics — the test asserts "≥3 layers" rather than
    "exactly 3" so a future perf sub-section does not break the gate.
    """
    text = _sweep_text()
    section = _perf_record_section(text)
    assert section.strip(), (
        f"`## 性能记录` section in {_SWEEP_MD} is empty; "
        "record at least the unit / integration / e2e baseline numbers"
    )

    layers = {m.group("layer").lower() for m in _LAYER_HEADING_RE.finditer(section)}
    required = {"unit", "integration", "e2e"}
    missing = required - layers
    assert not missing, (
        f"## 性能记录 is missing layer(s): {sorted(missing)!r}; "
        f"all three layers ({sorted(required)}) are required so a "
        f"wall-clock drift can be attributed to the layer that "
        f"actually regressed"
    )

    # For each required layer, the section that follows the heading
    # until the next heading must mention both a case-count metric and
    # a wall-clock metric.  Without both, a future contributor cannot
    # argue "this patch added N cases" or "this patch added Ms to the
    # wall-clock" — the section degrades into prose.
    for match in _LAYER_HEADING_RE.finditer(section):
        layer = match.group("layer").lower()
        if layer not in required:
            continue
        start = match.end()
        next_heading = re.search(
            r"^#{2,6}\s+", section[start:], flags=re.MULTILINE
        )
        end = start + next_heading.start() if next_heading else len(section)
        body = section[start:end]
        assert _CASE_COUNT_RE.search(body), (
            f"## 性能记录 `{layer}` block is missing a case-count "
            f"metric (用例数 / case count / N=<number>)"
        )
        assert _WALLCLOCK_RE.search(body), (
            f"## 性能记录 `{layer}` block is missing a wall-clock "
            f"metric (墙钟 / wall-clock / duration / <number>s|sec|min)"
        )
