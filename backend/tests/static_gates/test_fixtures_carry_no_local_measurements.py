"""The fixture directories carry no local measurement.

Why this gate exists
--------------------
Test-data decision 11 splits this project's test data in two: **structurally
identical synthetic data goes into the repository**, and the **real frozen
baseline stays on the operator's machine**, in a gitignored tree. The second
half is a convention, and conventions are what the first half quietly breaks:
one fixture copied out of a real plan directory carries the account name and
the dated plan id with it, and this repository has already published one
plan id that way.

So the three fixture directories get a gate of their own, and the gate is
built to the three properties a review habit does not have:

* it reads the **whole directory**, not the handful of files an integration
  test happens to know about -- a gate that only reads the files it was
  written for is a gate the next file walks straight past;
* it **fails** when a directory is missing instead of skipping, because a
  fresh clone that never received the fixtures must not come up green;
* its rules are **regression-tested against verbatim samples**, so a rule
  cannot quietly narrow itself to the one example that existed when it was
  written.

The rules
---------
1. **A home path that names a machine.** ``/Users/<account>/...`` and
   ``/home/<account>/...`` -- plus the Windows drive-letter form, which
   contains the same substring -- identify one developer's checkout. A path
   whose account segment is a documented placeholder does not, *except* when
   the path also carries a dated plan id: a placeholder account pointing at
   ``plans/<dated-id>`` is still a real plan directory, and the plan id is
   the private half of it.
2. **A real plan id.** ``<YYYYMMDD>-<slug>`` -- the shape an operator's plan
   directories are named after. The shape is shared with the sibling gates,
   so "what a plan id is" has exactly one definition.
3. **A dated first-hand measurement.** A measurement verb next to a date:
   the sentence reports what happened on one machine, not what the code
   does. The shape is shared with the sibling source-comment gate.

What this gate cannot see
--------------------------
A fixture whose *numbers* are one machine's numbers but whose identifiers
are all synthetic -- the desensitised effect-report baseline is exactly
that -- is **not** a violation, and this gate does not pretend otherwise.
The baseline's distribution is allowed to match the real one on purpose;
what must not ship is an identifier or a path that points at somebody's
machine. Nor can a gate read a fixture's meaning: only its text.

The concrete examples of every shape live in the tests below, assembled at
runtime rather than in this docstring -- see the note that follows.

Why this file must not import pytest
-------------------------------------
The sibling gates in this directory are imported by tools that run under a
bare interpreter (``scripts/check_commit_msg.py``, the pre-push hook), and
``pytest`` is installed in neither place. The tests below therefore use
plain ``assert``.

Why the samples in this file are assembled
------------------------------------------
This file is inside the scan root of the sibling gates. A whole
``/Users/<someone>/`` written here would fail the sibling home-path gate,
and a whole dated measurement would fail the sibling measurement gate --
so the samples that carry those shapes are built from fragments at runtime.
The text the rule sees is still the whole sample; only the way this file
spells it is different.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import source_scan
from test_no_plan_ids_in_non_test_source import PLAN_ID_SHAPE
from test_source_comments_carry_no_local_measurements import MEASUREMENT_SHAPES

# ---------------------------------------------------------------------------
# Where the fixtures live
# ---------------------------------------------------------------------------

#: The fixture root. ``backend/tests/static_gates/<this file>`` is two
#: parents below ``backend/tests/``, so the root is derived from this
#: file's own location and not from the process working directory -- CI
#: runs the suite with ``working-directory: backend``.
FIXTURES_ROOT: Path = Path(__file__).resolve().parents[1] / "fixtures"

#: The three directories the gate reads. Named rather than globbed, so that
#: a directory which stops existing is a *failure* instead of a silently
#: smaller scan.
FIXTURE_DIR_NAMES: tuple[str, ...] = (
    "verification",
    "effect_report",
    "failure_history",
)

#: The three directories, absolute.
FIXTURE_DIRS: tuple[Path, ...] = tuple(
    FIXTURES_ROOT / name for name in FIXTURE_DIR_NAMES
)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: Unix, macOS and Windows-drive-letter home prefixes. The **whole path**
#: is matched, not only the account segment, because this rule asks what the
#: path points at: ``/Users/a/b/plans/<dated-id>`` is a plan directory even
#: when the account segment is a placeholder.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^\s\"'`<>|]+")

#: Punctuation that is almost never the last character of a path, and that
#: is stripped so the reported path is the path rather than the sentence.
_TRAILING_PUNCTUATION = ".,;:!?)]}"

#: Account segments that do **not** identify a real machine. The set is
#: documented rather than generated, and it is the same list the sibling
#: home-path gate exempts: ``me`` is how a per-tenant command example names
#: its user's home, ``someone`` is the standard indefinite pronoun in API
#: docs, and ``...`` is the ellipsis convention for "wherever this lives".
PLACEHOLDER_HOME_SEGMENTS: frozenset[str] = frozenset({
    "a",
    "b",
    "x",
    "y",
    "u",
    "me",
    "user",
    "username",
    "you",
    "someone",
    "whoami",
    "name",
    "example",
    "your-name",
    "...",
})


def _home_path_segments(hit: str) -> list[str]:
    """The segments of a matched home path, after the account segment.

    ``/Users/<account>/a/b`` yields ``["<account>", "a", "b"]``; the
    Windows drive-letter form ``/C:/Users/<account>/a`` yields the same,
    because the match starts at ``/Users/``.
    """
    parts = [part for part in hit.split("/") if part]
    # parts[0] is "Users" or "home"; parts[1] is the account segment.
    return parts[1:]


def _is_placeholder_home_path(hit: str) -> bool:
    """True iff *hit* names no machine.

    Two conditions, and the second one is the interesting half:

    * the **account segment** is a documented placeholder (``me``, ``a``,
      ``example``, ...), so the path points at nobody in particular; and
    * the path carries **no date-prefixed plan id**. A placeholder account
      pointing at ``plans/<dated-id>`` is not a placeholder path -- the
      private thing in it is the plan id, and the account never was the
      point.
    """
    segments = _home_path_segments(hit)
    if not segments:
        return True
    if segments[0] not in PLACEHOLDER_HOME_SEGMENTS:
        return False
    return not PLAN_ID_SHAPE.search(hit)


def find_home_paths(text: str) -> list[str]:
    """Return every home path in *text* that identifies a specific machine.

    The returned strings are the matched paths themselves, so a caller can
    report where the path came from without re-running the rule. A path
    that names only a placeholder account is **not** returned -- see
    :func:`_is_placeholder_home_path` for the two conditions, and for the
    exception that catches a placeholder account pointing at a real plan
    directory.

    The same id is reported once per line: a path repeated inside one line
    is one piece of evidence, not two.
    """
    hits: list[str] = []
    for line in text.splitlines():
        seen: set[str] = set()
        for match in _HOME_PATH_RE.finditer(line):
            hit = match.group(0).rstrip(_TRAILING_PUNCTUATION)
            if not hit or hit in seen:
                continue
            seen.add(hit)
            if _is_placeholder_home_path(hit):
                continue
            hits.append(hit)
    return hits


def find_real_plan_ids(text: str) -> list[str]:
    """Return every date-prefixed plan id in *text*.

    A plan id is ``<YYYYMMDD>-<slug>`` -- the shape the sibling gates
    already own as :data:`PLAN_ID_SHAPE`, imported here so that "what a
    plan id is" has exactly one definition.

    Unlike the sibling rule for *source* files, there is no test-source
    exemption here: a fixture **is** test source, and that is precisely why
    the rule applies to it.

    The same id is reported once per line; the same id on two lines is two
    occurrences, because each is a place the reader has to go.
    """
    hits: list[str] = []
    for line in text.splitlines():
        seen: set[str] = set()
        for match in PLAN_ID_SHAPE.finditer(line):
            plan_id = match.group(0)
            if plan_id in seen:
                continue
            seen.add(plan_id)
            hits.append(plan_id)
    return hits


def find_local_measurements(
    path: Path, text: str
) -> list[tuple[Path, int, str]]:
    """Return ``(path, lineno, match)`` for every dated first-hand measurement.

    The shapes are the sibling source-comment gate's, imported rather than
    restated: a measurement verb next to a date, and the scar of a
    mechanical rewrite. One definition, so the two gates cannot drift into
    disagreeing about what a local measurement is.

    One entry per line per shape, so a caller can format
    ``rel:lineno: <match>`` without re-walking the tree.
    """
    hits: list[tuple[Path, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for shape in MEASUREMENT_SHAPES:
            for match in shape.finditer(line):
                hits.append((path, lineno, match.group(0).strip()))
    return hits


# ---------------------------------------------------------------------------
# Reading the fixtures
# ---------------------------------------------------------------------------


def iter_fixture_files() -> Iterator[Path]:
    """Yield every file under the three fixture directories, in a stable order."""
    for directory in FIXTURE_DIRS:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def read_fixture(path: Path) -> str:
    """The fixture's text. Undecodable bytes are replaced, not fatal."""
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The samples the rules are pinned against
# ---------------------------------------------------------------------------

#: An account segment that names a machine. Assembled from two fragments so
#: that this file's own source does not contain a non-placeholder home path
#: -- the sibling home-path gate scans every first-party file, this one
#: included, and a whole ``/Users/<someone>/`` written here would fail it.
_MACHINE_ACCOUNT = "zhang" + "san"

#: The sample the rule is pinned against: a plan directory that names both
#: a home path and a dated plan id. Verbatim -- it is the shape that
#: leaked, and a rule tested only against paraphrases is a rule tested
#: against nothing.
KNOWN_SAMPLE = '{"plan_dir": "/Users/a/b/plans/20260101-x"}'

#: Verbatim samples that name a machine. Each row is a line of text as it
#: would appear in a fixture, not a description of a shape.
MACHINE_HOME_PATH_SAMPLES: tuple[str, ...] = (
    KNOWN_SAMPLE,
    'cwd = "/Use' + 'rs/' + _MACHINE_ACCOUNT + '/work/repo"',
    "plan_dir: /home/" + _MACHINE_ACCOUNT + "/plans",
)

#: Verbatim samples that name no machine and must therefore be let
#: through. The first row is the boundary the task draws: a path made of
#: placeholder segments identifies nobody.
PLACEHOLDER_HOME_PATH_SAMPLES: tuple[str, ...] = (
    "/Users/a/b",
    "/Users/me/Documents/notes.txt",
    "/home/user/project/src",
    "/Users/example/repo/.venv/bin/pytest",
)

#: Verbatim samples that carry a dated plan id.
PLAN_ID_SAMPLES: tuple[str, ...] = (
    "20260101-x",
    "plan_dir: plans/20260101-example-topic",
    "see 20260926-example-topic for the worked example",
)

#: A dated first-hand measurement. The date lives in its own literal, on
#: its own line, so that this file's own source never holds a measurement
#: verb next to a date -- the sibling gate that owns that shape scans every
#: first-party file, this one included.
_MEASUREMENT_DATE = "2026-10-" + "01"

#: The measurement sample, verbatim: a verb, a date, and a result.
MEASUREMENT_SAMPLE = (
    "Observed on the " + _MEASUREMENT_DATE + " the endpoint returned 200"
)

#: Text that looks like a plan id and is not. A rule with no counterweight
#: is a rule that has only been tested in one direction.
NOT_A_PLAN_ID_SAMPLES: tuple[str, ...] = (
    "released 2026-10-14 as build 20261014",
    "plan_id: sample-plan-001",
    "plan_id: vp-fixture-001",
    "the plan count went 15 -> 35 tasks",
    "build 20260101",
)


# ---------------------------------------------------------------------------
# 1. The fixture directories exist
# ---------------------------------------------------------------------------


def test_fixture_dirs_exist() -> None:
    """The three fixture directories exist and are not empty.

    A missing directory is a failure, not a skip: a fresh clone that never
    received the fixtures must not report a clean run.
    """
    for directory in FIXTURE_DIRS:
        # The path is reported repository-relative: the absolute form
        # would print the runner's own checkout -- a home path -- into the
        # failure message of the gate that exists to keep home paths out of
        # the tree.
        rel = source_scan.repo_relative(directory)
        assert directory.is_dir(), (
            f"missing fixture directory: {rel} -- the gate must fail, "
            "not skip, or a clone without the fixtures comes up green"
        )
        files = [
            path
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        assert files, f"fixture directory is empty: {rel}"


# ---------------------------------------------------------------------------
# 2. No home path names a machine
# ---------------------------------------------------------------------------


def test_no_home_paths() -> None:
    """No fixture carries a home path that identifies a machine."""
    offenders: list[str] = []
    scanned = 0

    for path in iter_fixture_files():
        scanned += 1
        for lineno, line in enumerate(read_fixture(path).splitlines(), start=1):
            for hit in find_home_paths(line):
                # The matched path names somebody's account and directory.
                # Withheld; ``path:lineno`` is what a reader needs.
                offenders.append(
                    f"{source_scan.repo_relative(path)}:{lineno}: "
                    f"home path {source_scan.redact_span(hit)}"
                )

    assert scanned, (
        "the fixture walk returned no file -- the gate would now pass on an "
        "empty result."
    )
    assert not offenders, (
        "A fixture carries a home path that identifies a specific machine. "
        "Replace it with a placeholder account (/Users/me/...), or with a "
        "path relative to the repository.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 3. No real plan id
# ---------------------------------------------------------------------------


def test_no_real_plan_ids() -> None:
    """No fixture carries a date-prefixed plan id."""
    offenders: list[str] = []
    scanned = 0

    for path in iter_fixture_files():
        scanned += 1
        for lineno, line in enumerate(read_fixture(path).splitlines(), start=1):
            for plan_id in find_real_plan_ids(line):
                # The plan id is the private name itself: the directory an
                # operator's plan was actually run in.
                offenders.append(
                    f"{source_scan.repo_relative(path)}:{lineno}: "
                    f"plan id {source_scan.redact_span(plan_id)}"
                )

    assert scanned, (
        "the fixture walk returned no file -- the gate would now pass on an "
        "empty result."
    )
    assert not offenders, (
        "A fixture carries a date-prefixed plan id. Use a synthetic id "
        "(vp-fixture-001, sample-plan-001) -- a real one names a plan "
        "directory that ran on one machine.\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 4. No dated first-hand measurement
# ---------------------------------------------------------------------------


def test_no_local_measurements() -> None:
    """No fixture carries a dated first-hand measurement."""
    offenders: list[str] = []
    scanned = 0

    for path in iter_fixture_files():
        scanned += 1
        for _, lineno, matched in find_local_measurements(path, read_fixture(path)):
            offenders.append(
                f"{source_scan.repo_relative(path)}:{lineno}: "
                f"measurement {source_scan.redact_span(matched)}"
            )

    assert scanned, (
        "the fixture walk returned no file -- the gate would now pass on an "
        "empty result."
    )
    assert not offenders, (
        "A fixture carries a dated first-hand measurement. Keep the shape of "
        "the data; drop the date and the numbers that came with it.\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 5. The rules still catch what they were written for
# ---------------------------------------------------------------------------


def test_rule_detects_known_sample() -> None:
    """The known sample is reported: a home path and a dated plan id."""
    assert find_home_paths(KNOWN_SAMPLE) == [
        "/Users/a/b/plans/20260101-x"
    ], KNOWN_SAMPLE
    assert find_real_plan_ids(KNOWN_SAMPLE) == ["20260101-x"], KNOWN_SAMPLE


def test_the_rule_covers_more_than_the_known_sample() -> None:
    """Verbatim samples beyond the known one are reported too.

    A rule regression-tested against a single example is a rule that only
    has to recognise that example.
    """
    for sample in MACHINE_HOME_PATH_SAMPLES:
        assert find_home_paths(sample), f"not detected: {sample!r}"
    for sample in PLAN_ID_SAMPLES:
        assert find_real_plan_ids(sample), f"not detected: {sample!r}"


def test_placeholder_paths_are_left_alone() -> None:
    """A path made of placeholder segments identifies nobody."""
    for sample in PLACEHOLDER_HOME_PATH_SAMPLES:
        assert find_home_paths(sample) == [], f"flagged: {sample!r}"


def test_near_misses_are_not_reported() -> None:
    """Text that looks like a plan id and is not stays clean."""
    for sample in NOT_A_PLAN_ID_SAMPLES:
        assert find_real_plan_ids(sample) == [], f"flagged: {sample!r}"


def test_the_measurement_rule_detects_a_dated_measurement() -> None:
    """A measurement verb next to a date is reported, with its line."""
    hits = find_local_measurements(Path("dummy.json"), MEASUREMENT_SAMPLE)
    assert hits, f"not detected: {MEASUREMENT_SAMPLE!r}"
    assert hits[0][1] == 1, hits
