"""Static gates for the verification-mode change surface.

Why this gate exists
--------------------
Adding a module can break the repository's own rules in four ways that
no functional test can see. The code still runs, the behaviour tests
still pass, and the repository has quietly stopped being the thing its
rules say it is:

1. **A pull-request number written into the source.** The number is a
   pointer at one review round. Renumbering the pull request, opening a
   replacement, or back-porting the change leaves the pointer aimed at a
   different discussion, and nothing notices because the string still
   parses. The number belongs in the commit message and the pull-request
   title, where the platform keeps it in step; it does not belong in a
   comment, a docstring, a default or a prompt template.
2. **A local-machine home path.** A path under one machine's home
   directory works on that machine and nowhere else, and it identifies
   the machine to every reader of the source.
3. **A new third-party dependency.** The new module imports a library
   that nothing declared, so a clean install of the declared dependency
   set does not contain it and the failure arrives in CI rather than in
   review.
4. **A change to a module the feature was told not to touch.** The
   frontend, the state machine, the verification loop and the
   verification API runner are shared with the rest of the product; a
   change made for the feature's convenience lands on everyone else.

None of the four shows up in a functional test: the code works, the
tests pass, and the repository has quietly stopped being the thing the
repository's own rules say it is. These gates are the ones that say so.

What each gate checks
---------------------
``test_no_pr_number_in_sources``
    No pull-request number in the backend production sources — not in a
    comment, not in a docstring. Docstrings are not exempt: a number
    inside a docstring is still a number that has to be edited when the
    pull request is renumbered, and a docstring is where such a number
    survives longest, because nobody reads a docstring while reviewing
    a diff.

``test_no_absolute_home_paths``
    No home path in the backend production sources. Two strengths: the
    repository's canonical rule (no home path that *names a machine*)
    over the whole tree, and the literal form (no ``/Users/`` and no
    ``/home/`` at all) over the verification-mode modules, where no
    legacy file is quoting the rule itself.

``test_requirements_hash_frozen``
    The declared dependency set is frozen by hash. A new third-party
    dependency changes the hash and the gate says so.

``test_schema_ddl_hash_frozen``
    The state database schema is frozen by hash, so a new table or a
    new column is a visible change rather than a silent migration.

``test_forbidden_modules_unchanged``
    The change surface is empty: the modules the verification mode was
    told not to touch are identical to their state on ``main``.

Two strengths, and why each of them is needed
---------------------------------------------
A single rule applied to a single scope is either too weak to catch the
defect or too noisy to be worth running. Two of the rules here are
therefore applied twice, at two strengths, over two scopes:

* **The whole backend production tree** gets the rule with the floors
  that keep it off the tree's own documentation. The backend docstrings
  use ``#1``, ``#2`` and ``#3`` as section anchors pointing at each
  other, and they name numbered steps in a plan; a rule that flagged
  those would fire on forty unrelated lines and would be switched off.
* **The verification-mode modules** get the rule without those floors.
  On that surface the ambiguity does not arise, so nothing new can slip
  in with a short number — and a module added by a later task joins the
  surface on its own, because the scope is a path test rather than a
  hand-kept list.

Scope of the source scan
------------------------
The source scan covers ``backend/**/*.py`` and excludes the test trees:
a synthetic fixture in a test is not the leak this gate is written
against, and a gate that fires on fixtures is a gate that gets switched
off. The dependency and schema gates read the manifest and the live
migration rather than a copy of either, so they describe the artifacts
the product actually installs and the schema it actually opens.

The frozen values are constants in this file. Changing one is a
deliberate act: the diff says "the dependency set changed" or "the
schema changed", and the reviewer sees it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

#: Repository root. This file lives at
#: ``backend/tests/static_gates/``, so the root is three parents up.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The backend package root.
BACKEND_DIR: Path = REPO_ROOT / "backend"

#: The dependency manifest. This repository replaced
#: ``backend/requirements.txt`` with ``backend/pyproject.toml`` — the
#: same artifact under a newer name. The sibling gate
#: ``test_dependency_manifest_and_lock_agree.py`` is what keeps the
#: manifest and ``backend/uv.lock`` in step; this gate answers the
#: other question, which is whether *anything* was added to the
#: declared set at all.
DEPENDENCY_MANIFEST: Path = BACKEND_DIR / "pyproject.toml"

#: Path segments that are never first-party production source: the
#: virtual environment (a symlink into another checkout), the package
#: caches, and the test trees.
EXCLUDED_SEGMENTS: frozenset[str] = frozenset({
    ".venv",
    "node_modules",
    "__pycache__",
    "tests",
})

#: The modules the verification mode is not allowed to change. They are
#: shared with the rest of the product: the state machine owns plan
#: state, the verification loop owns the run, and the API runner owns
#: the process boundary. A change made for the feature's convenience
#: lands on everyone else, and the feature's own tests cannot see it.
#:
#: The state machine's **test** tree is excluded on purpose. This gate
#: measures production behaviour; those tests pin the HTTP contract the
#: feature is allowed to extend, so a permitted production change has to
#: be able to reach its own contract test. The production tree below is
#: still frozen byte for byte.
FORBIDDEN_MODULE_PATHS: tuple[str, ...] = (
    "frontend",
    "backend/state_machine",
    "backend/verification_loop.py",
    "backend/verification_api_runner.py",
    ":(exclude)backend/state_machine/tests",
)

#: The base ref the change surface is measured against, in preference
#: order. A local clone has ``main``; a CI checkout of a pull request
#: has ``origin/main`` when the base was fetched.
BASE_REF_CANDIDATES: tuple[str, ...] = ("main", "origin/main")

# ---------------------------------------------------------------------------
# The pull-request number rule
# ---------------------------------------------------------------------------

#: Pull-request numbers that must never appear as a literal in the
#: source. The number is listed by name rather than only by shape so
#: that the concrete number is checked even where it appears without a
#: marker — a bare number in a comment is still a pull-request number.
FORBIDDEN_PR_NUMBERS: frozenset[str] = frozenset({"8456"})

#: The marked forms of a pull-request reference: ``PR #42``,
#: ``pull request 42``, ``merge request 42``, ``gh pr 42``,
#: ``pull/42``. The marker is what makes the number unambiguous, so
#: this is the form that is checked at full strength on the
#: verification-mode surface.
PR_MARKED_STRICT_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:"
    r"\b(?:pr|pull\s+request|merge\s+request|gh\s+pr)\b[\s]*#?[\s]*(\d+)\b"
    r"|\bpull/(\d+)\b"
    r")"
)

#: The same marked forms with a two-digit floor, for the whole-tree
#: scan. One digit is excluded because the backend docstrings refer to
#: numbered steps in a plan that way, and a rule that flagged those
#: would be reporting a plan's step list as a pull-request reference.
PR_MARKED_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:"
    r"\b(?:pr|pull\s+request|merge\s+request|gh\s+pr)\b[\s]*#?[\s]*(\d{2,})\b"
    r"|\bpull/(\d{2,})\b"
    r")"
)

#: The bare hash form — ``#8456`` — with a three-digit floor. The floor
#: is higher than the marked form's because the bare form is *not*
#: unambiguous: the backend docstrings use ``#1``, ``#2`` and ``#3`` as
#: section anchors pointing at each other, and those are the most
#: common number-shaped token in the tree by a wide margin. A
#: three-digit number is far enough above that vocabulary to mean what
#: it says.
PR_HASH_RE: re.Pattern[str] = re.compile(r"(?<![\w#])#(\d{3,})\b")

#: The forbidden literals, matched on their own rather than as a
#: substring: a number that is part of a longer token (a hash, a
#: version, an identifier) is not a pull-request number.
FORBIDDEN_PR_LITERAL_RE: re.Pattern[str] = re.compile(
    r"(?<![\w])(?:" + "|".join(FORBIDDEN_PR_NUMBERS) + r")(?![\w])"
)

# ---------------------------------------------------------------------------
# The home-path rule
# ---------------------------------------------------------------------------

#: The literal prefixes of a local home directory. Over the whole
#: backend tree the repository's canonical rule is used instead (see
#: ``find_home_paths`` below), because three legacy files quote the rule
#: itself in their own text. Over the verification-mode modules there is
#: no such legacy text, so the literal form is used there.
RAW_HOME_PREFIXES: tuple[str, ...] = ("/Users/", "/home/")


# ---------------------------------------------------------------------------
# Import the canonical home-path rule
# ---------------------------------------------------------------------------
#
# The home-path rule has exactly one definition, in the sibling gate
# ``test_no_local_home_path_in_first_party.py``. Keeping a second copy
# here would let the two drift, and a drifted copy is how a gate stops
# catching anything. The sibling module deliberately imports no pytest,
# so importing it here costs nothing at module scope.

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(BACKEND_DIR))

from test_no_local_home_path_in_first_party import (  # noqa: E402
    find_home_paths,
)
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def iter_backend_sources() -> list[Path]:
    """Every production ``backend/**/*.py`` file, test trees excluded.

    Sorted, so the failure message lists the offenders in the same
    order on every machine. Absolute paths, so the caller does not
    depend on where the process was started.
    """
    sources: list[Path] = []
    for path in BACKEND_DIR.rglob("*.py"):
        rel = path.relative_to(BACKEND_DIR)
        if EXCLUDED_SEGMENTS & set(rel.parts):
            continue
        sources.append(path)
    return sorted(sources)


def verification_surface() -> list[Path]:
    """The scanned modules that belong to the verification mode.

    Every scanned source with a path part that names the verification
    mode: the ``verification`` package, the ``verification_*`` modules,
    ``routes/verification.py`` and the verification repository. The
    definition is a path test rather than a hand-kept list, so a module
    added by a later task joins the surface on its own and cannot be
    forgotten.
    """
    return [
        path
        for path in iter_backend_sources()
        if any(
            part.startswith("verification")
            for part in path.relative_to(BACKEND_DIR).parts
        )
    ]


def find_pr_numbers(text: str, *, strict: bool = False) -> list[tuple[int, str]]:
    """Return ``(lineno, number)`` for every pull-request number in *text*.

    *strict* selects the marked forms without a digit floor; it is what
    the verification-mode surface is checked with, where the numbered
    steps of a plan do not occur.

    A number is reported once per line, whichever shape it was written
    in: ``PR #8456`` matches both the marked form and the hash form,
    and the reader wants one line, not two.
    """
    marked = PR_MARKED_STRICT_RE if strict else PR_MARKED_RE
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen: set[str] = set()
        for match in PR_HASH_RE.finditer(line):
            number = match.group(1)
            if number in seen:
                continue
            seen.add(number)
            hits.append((lineno, number))
        for match in marked.finditer(line):
            number = next(
                group for group in match.groups() if group is not None
            )
            if number in seen:
                continue
            seen.add(number)
            hits.append((lineno, number))
        for match in FORBIDDEN_PR_LITERAL_RE.finditer(line):
            number = match.group(0)
            if number in seen:
                continue
            seen.add(number)
            hits.append((lineno, number))
    return hits


def declared_requirements() -> list[str]:
    """Every requirement string the backend manifest declares.

    The runtime dependencies and the dependency groups together: a
    third-party package is a third-party package whichever group it is
    declared in, and a tool that only the tests use is still something
    a clean install has to provide.
    """
    data = tomllib.loads(DEPENDENCY_MANIFEST.read_text(encoding="utf-8"))
    project = data["project"]
    requirements: list[str] = list(project.get("dependencies", []))
    groups = data.get("dependency-groups", {})
    for name in sorted(groups):
        requirements.extend(groups[name])
    return sorted(requirements)


def requirements_hash() -> str:
    """The sha256 of the declared requirement set.

    Sorted requirement strings, one per line. Hashing the *set* rather
    than the file means a comment edit or a reordering does not move
    the hash, while an added package, a removed package or a changed
    version floor does.
    """
    payload = "\n".join(declared_requirements())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_ddl_objects(workdir: Path) -> list[tuple[str, str, str]]:
    """The ``(type, name, sql)`` rows of a freshly migrated state db.

    The database is built with the production helpers — the same
    ``open`` the server uses and the same ``migrate`` the schema gate
    hashes — so what is hashed is the schema the product actually
    opens, not a copy of the DDL that could drift away from it.
    """
    conn = open_db(workdir / "state.db")
    try:
        migrate(conn)
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1], row[2]) for row in rows]


def schema_ddl_hash(workdir: Path) -> str:
    """The sha256 of the state db schema.

    The stored ``CREATE`` text of every user object, in a fixed order.
    SQLite rewrites that text when a column is added, so a new column
    moves the hash just as a new table does.
    """
    payload = "\n".join(
        f"{obj_type}\t{name}\t{sql}"
        for obj_type, name, sql in schema_ddl_objects(workdir)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_base_ref() -> str | None:
    """The ref the change surface is measured against, or ``None``.

    ``None`` means neither candidate resolves — a checkout that has no
    base ref at all. The caller decides what to do about that; it must
    not decide "therefore the surface is empty".
    """
    for candidate in BASE_REF_CANDIDATES:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return None


def forbidden_modules_diff(base_ref: str) -> subprocess.CompletedProcess[str]:
    """The diff of the forbidden modules against the branch's merge base.

    The merge base is what makes this the *branch's* change surface:
    commits that landed on *base_ref* after this branch forked are not
    changes this branch made, and diffing against the base tip reports
    them as deletions as soon as the base is fast-forwarded.

    Run from ``backend/`` with ``git -C ..``, so the pathspecs are
    repository-root-relative and the result does not depend on where
    the test process was started.
    """
    merge_base = subprocess.run(
        ["git", "-C", "..", "merge-base", base_ref, "HEAD"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return subprocess.CompletedProcess(
            args=merge_base.args,
            returncode=merge_base.returncode or 1,
            stdout="",
            stderr=(
                f"cannot resolve a merge base against {base_ref}: "
                f"{merge_base.stderr.strip()}"
            ),
        )
    return subprocess.run(
        [
            "git",
            "-C",
            "..",
            "diff",
            "--exit-code",
            merge_base.stdout.strip(),
            "--",
            *FORBIDDEN_MODULE_PATHS,
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


def _relative(path: Path) -> str:
    """*path* as a repository-root-relative string, for messages."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# The frozen values
# ---------------------------------------------------------------------------

#: The declared dependency set, frozen. A third-party dependency added
#: to ``backend/pyproject.toml`` changes this value, and the gate says
#: so instead of leaving it to a reviewer to notice.
FROZEN_REQUIREMENTS_HASH: str = (
    "a0bf3bba5487fe7aa35de6340476f231caa58f1baea5cdccbc917d15766165eb"
)

#: The state db schema, frozen. A new table, a new column or a new
#: index changes this value.
FROZEN_SCHEMA_DDL_HASH: str = (
    "bc821cabc9b8ec6d629a18aa4d036873478c66773353a97b5bf87e257750dd51"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_target_is_not_empty() -> None:
    """The scan must reach real source.

    A gate whose scan target has disappeared — the excluded-segment set
    swallowed the tree, the backend moved — passes vacuously. This
    test fails the moment that happens, so the regression is visible
    rather than silent.
    """
    sources = iter_backend_sources()
    assert sources, (
        "iter_backend_sources() returned no files — the gate would now "
        "pass on an empty result. Either EXCLUDED_SEGMENTS or BACKEND_DIR "
        "has drifted away from the real backend tree."
    )
    assert verification_surface(), (
        "verification_surface() returned no files — the strict checks "
        "would now pass on an empty result."
    )


def test_pr_number_in_a_docstring_is_a_violation() -> None:
    """A pull-request number in a docstring is still a violation.

    Docstrings are not exempt. A number written in prose survives a
    renumbering just as a number written in a comment does, and a
    docstring is where it survives longest.

    The number in the sample is not one of the forbidden literals, so
    what is being exercised here is the *shape* rule rather than the
    literal list.
    """
    number = "91" + "37"
    sample = (
        "def start():\n"
        '    """Start the loop.\n'
        "\n"
        "    See PR #" + number + " for the original discussion.\n"
        '    """\n'
    )
    hits = find_pr_numbers(sample, strict=True)
    assert [found for _, found in hits] == [number], hits


def test_forbidden_pr_number_is_caught_without_a_marker() -> None:
    """A forbidden number is a violation with no marker around it.

    The forbidden list exists because a shape rule cannot recognise a
    bare number: ``8456`` in a comment is a pull-request number and
    nothing else, and no regular expression can tell it apart from a
    port or a timestamp.
    """
    number = "84" + "56"
    sample = f"the change landed as {number}\n"
    hits = find_pr_numbers(sample)
    assert [found for _, found in hits] == [number], hits


def test_home_path_naming_a_machine_is_a_violation() -> None:
    """The canonical home-path rule has teeth.

    The sample is assembled from fragments so this file's own source
    does not contain the literal the rule forbids — a self-referential
    scan would either suppress the rule for this file (the gate stops
    catching its own authors) or fail the suite on the gate's own
    definition.
    """
    text = 'cwd = "/Use' + 'rs/kai/work/repo"\n'
    assert find_home_paths(Path("dummy.py"), text) == [
        (Path("dummy.py"), 1, "kai")
    ]


def test_no_pr_number_in_sources() -> None:
    """No pull-request number in the backend production sources.

    The number is a pointer at one review round. Docstrings and
    comments are not exempt: a number written there is still a number
    that has to be edited when the pull request is renumbered, and a
    docstring is the place where it survives longest, because nobody
    reads a docstring while reviewing a diff.

    The whole tree is scanned with the digit floors that keep the rule
    off the tree's own documentation, and the verification-mode surface
    is scanned again without them.
    """
    offenders: dict[tuple[str, int, str], None] = {}

    def record(path: Path, lineno: int, number: str) -> None:
        offenders.setdefault((_relative(path), lineno, number), None)

    for path in iter_backend_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, number in find_pr_numbers(text):
            record(path, lineno, number)

    for path in verification_surface():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, number in find_pr_numbers(text, strict=True):
            record(path, lineno, number)

    listed = [
        f"{path}:{lineno}: pull-request number {number}"
        for path, lineno, number in offenders
    ]
    assert not listed, (
        "Backend production source carries a pull-request number. The "
        "number belongs in the commit message and the pull-request "
        "title, where the platform keeps it in step; a number in the "
        "source survives a renumbering and points at a different "
        "discussion. Remove it, or replace it with a placeholder that "
        "names no particular pull request.\n  " + "\n  ".join(listed)
    )


def test_no_absolute_home_paths() -> None:
    """No local home path in the backend production sources.

    Two strengths, because one is not enough:

    * The repository's canonical rule over the whole tree — no home
      path whose segment names a machine.
    * The literal form over the verification-mode modules — no
      ``/Users/`` and no ``/home/`` at all. Three legacy backend files
      quote the rule itself in their own text, which is why the literal
      form is scoped to the verification-mode surface rather than
      applied to the whole tree; on that surface there is no legacy
      text to exempt.
    """
    offenders: list[str] = []
    for path in iter_backend_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for _, lineno, _segment in find_home_paths(path, text):
            offenders.append(
                f"{_relative(path)}:{lineno}: home path naming a machine"
            )

    for path in verification_surface():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for prefix in RAW_HOME_PREFIXES:
                if prefix in line:
                    offenders.append(
                        f"{_relative(path)}:{lineno}: literal {prefix} prefix"
                    )

    assert not offenders, (
        "Backend production source carries a local-machine home path. "
        "A path under one machine's home directory works on that "
        "machine and nowhere else, and it identifies the machine to "
        "every reader of the source. Strip the path, replace it with a "
        "placeholder, or move the value into a runtime config or an "
        "environment variable.\n  " + "\n  ".join(offenders)
    )


def test_requirements_hash_frozen() -> None:
    """The declared dependency set is unchanged.

    A new module that imports a library nothing declared installs
    cleanly on the developer's machine and fails in CI, one lane at a
    time, with an error that points at the import rather than at the
    manifest.
    """
    declared = declared_requirements()
    assert declared, (
        "the manifest declares no requirements at all — the hash would "
        "be the hash of an empty string and the gate would prove "
        "nothing. Check DEPENDENCY_MANIFEST."
    )

    current = requirements_hash()
    assert current == FROZEN_REQUIREMENTS_HASH, (
        "the declared dependency set changed. The verification mode "
        "adds no third-party dependency; a change here means a package "
        "was added, removed or re-pinned.\n"
        f"  frozen: {FROZEN_REQUIREMENTS_HASH}\n"
        f"  current: {current}\n"
        f"  declared ({len(declared)}): {declared}"
    )


def test_schema_ddl_hash_frozen(tmp_path: Path) -> None:
    """The state db schema is unchanged.

    A new table or a new column is a schema migration, and a schema
    migration is a decision about what the state machine owns. The
    verification mode was told to work through the existing schema, so
    the shape is frozen and a change to it has to be deliberate.
    """
    objects = schema_ddl_objects(tmp_path)
    assert objects, (
        "the migrated database holds no schema objects — the hash would "
        "be the hash of an empty string and the gate would prove "
        "nothing."
    )

    current = schema_ddl_hash(tmp_path)
    assert current == FROZEN_SCHEMA_DDL_HASH, (
        "the state db schema changed. The verification mode adds no "
        "table and no column; a change here means a migration was "
        "introduced.\n"
        f"  frozen: {FROZEN_SCHEMA_DDL_HASH}\n"
        f"  current: {current}\n"
        f"  objects: {[(obj_type, name) for obj_type, name, _ in objects]}"
    )


def test_forbidden_modules_unchanged() -> None:
    """The change surface is empty.

    The frontend, the state machine, the verification loop and the
    verification API runner are shared with the rest of the product. A
    change made for the verification mode's convenience lands on
    everyone else, and the verification mode's own tests cannot see it.
    """
    base_ref = resolve_base_ref()
    if base_ref is None:
        import pytest

        pytest.skip(
            "no base ref available: neither "
            f"{' nor '.join(BASE_REF_CANDIDATES)} resolves in this "
            "checkout, so the change surface cannot be measured. Fetch "
            "the base branch and re-run — a gate that cannot measure "
            "its surface must say so rather than report it as empty."
        )

    result = forbidden_modules_diff(base_ref)
    assert result.returncode == 0, (
        f"the forbidden modules differ from {base_ref}:\n"
        f"{result.stdout}"
        f"git exited {result.returncode}: {result.stderr}"
    )
    assert result.stdout.strip() == "", (
        f"the forbidden modules differ from {base_ref}:\n{result.stdout}"
    )