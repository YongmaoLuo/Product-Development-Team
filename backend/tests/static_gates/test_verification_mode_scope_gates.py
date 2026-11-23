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
#: The state machine's ``backend/state_machine/tests`` tree is excluded
#: on purpose. This gate measures production behaviour; those tests pin
#: the HTTP contract the feature is allowed to extend, so a permitted
#: production change has to be able to reach its own contract test. The
#: production tree below is still frozen byte for byte.
#:
#: Three files are excluded for the phase merge that renamed the
#: waiting bucket to ``queued``: ``frontend/app.js``,
#: ``frontend/style.css`` and ``backend/verification_loop.py``. The
#: rename is a product decision taken outside the verification mode and
#: it lands on shared code, so the change-surface gate has to be told
#: about it rather than kept silent. Excluding the three files from
#: the diff is only half the answer: their contents are frozen byte for
#: byte by ``FROZEN_R10_SURFACE_SHA256`` and checked by
#: ``test_r10_waiting_phase_rename_surface_is_frozen``, so a later edit
#: to any of them fails a gate even though it no longer shows up in the
#: diff. The exclusion is scoped to these three paths only — the rest
#: of ``frontend/``, ``backend/state_machine/`` and
#: ``backend/verification_api_runner.py`` stay frozen inside the diff.
#:
#: One more file is excluded for the capacity-wait work:
#: ``backend/state_machine/repositories/execution_repository.py``.  The
#: executor can park a run because the provider has no quota left, and
#: that disposition has to be a word the repository knows — the
#: five-value status set had no word for it, and a caller persisting
#: the truth had to write ``running`` (so a restart would probe the
#: PID, find it gone and report the pause as a crash) or ``stopped``
#: (which reads as "the operator cancelled this").  The exclusion is
#: scoped to that one file, and its bytes are frozen by
#: ``FROZEN_CAPACITY_WAIT_SURFACE_SHA256`` (checked by
#: ``test_capacity_wait_status_surface_is_frozen``), so the rest of
#: ``backend/state_machine/`` stays frozen inside the diff.
#:
#: One more file is excluded for the root-cause report delivery
#: channel (2026-11-04):
#: ``backend/state_machine/repositories/plan_task_repository.py``.  The
#: report-style task needs its deliverable to be a file and its address
#: to live in ``report_path``, so ``ALLOWED_STATIC_TASK_FIELDS`` there
#: has to carry ``deliverable`` / ``report_path``.  Following the
#: ``execution_repository.py`` precedent, this is a narrow exclusion
#: scoped to that one file; the rest of its content, and the rest of
#: ``backend/state_machine/``, stay frozen inside the diff.
#:
#: Two more files are excluded for the native scheduler:
#: ``backend/state_machine/db/schema.py`` (schema v7 adds the three
#: ``scheduler_*`` tables) and ``frontend/index.html`` (the scheduler
#: view's markup).  Both are scoped exclusions whose bytes are frozen
#: by ``FROZEN_SCHEDULER_SURFACE_SHA256`` (checked by
#: ``test_scheduler_surface_is_frozen``), so the rest of
#: ``backend/state_machine/`` and ``frontend/`` stays frozen inside the
#: diff.
#:
#: One more file is excluded for the base drift round record
#: (2026-11-14) — the verification round repository:
#: ``backend/state_machine/repositories/verification_repository.py``.
#: A round that base drift blocks has to leave one auditable round row
#: behind (``mark_round_blocked``), and that write path lives on the
#: verification round repository.  The change is that one method and its
#: audit fields, and the behaviour is pinned line by line by
#: ``test_verification_repository_round_blocked.py`` — so this
#: exclusion is carried by a behavioural pin rather than by the
#: byte-for-byte freeze the surfaces above use.  Either way the rest of
#:
#: One more file is excluded for the round-release work:
#: ``backend/state_machine/repositories/verification_repository.py``.
#: A round that measured nothing must give its round number back, and
#: the counter lives in that repository — ``init_round`` overwrites it
#: and there was no way back, so a plan burned one of its rounds on a
#: turn that never produced a verdict.  ``release_round`` is that way
#: back (and ``_decode_executor_state`` is the tolerant reader it needs
#: to merge into the free-form ``executor_state`` blob).  The exclusion
#: is scoped to that one file, and its bytes are frozen by
#: ``FROZEN_VERIFICATION_REPOSITORY_SURFACE_SHA256`` (checked by
#: ``test_verification_repository_surface_is_frozen``), so the rest of
#: ``backend/state_machine/`` stays frozen inside the diff.
#:
#: One more file is excluded for the verification-mirror removal — the
#: routing repository:
#: ``backend/state_machine/repositories/routing_repository.py``.
#: This plan moved the authoritative write of the verification state out
#: of the routing row's ``verification`` mirror column: ``write_plan_state``
#: no longer writes that column, and the one thing the column still
#: records — the frozen baseline — has exactly one writer
#: (``write_verification_baseline``).  The change is that column's
#: ownership, and it reaches shared code, so the exclusion is scoped to
#: that one file; the rest of ``backend/state_machine/`` stays frozen
#: inside the diff.  Its bytes are frozen by
#: ``FROZEN_ROUTING_REPOSITORY_SURFACE_SHA256`` (checked by
#: ``test_routing_repository_surface_is_frozen``), so a later edit to it
#: fails a gate even though it no longer shows up in the diff.
#:
#: The pathspecs are repository-root-relative: the helper runs
#: ``git -C ..`` from ``backend/``, so the prefix is the repository
#: root.
FORBIDDEN_MODULE_PATHS: tuple[str, ...] = (
    "frontend",
    "backend/state_machine",
    "backend/verification_loop.py",
    "backend/verification_api_runner.py",
    ":(exclude)backend/state_machine/tests",
    ":(exclude)frontend/app.js",
    ":(exclude)frontend/style.css",
    ":(exclude)frontend/index.html",
    ":(exclude)backend/verification_loop.py",
    ":(exclude)backend/state_machine/repositories/execution_repository.py",
    ":(exclude)backend/state_machine/repositories/plan_task_repository.py",
    # 2026-11-14：base drift 阻塞的轮次必须留下一行可审计的轮次记录
    # （`mark_round_blocked`），这条写入路径落在验证轮次仓库上。改动只有
    # 这一个方法与它的审计字段，行为由
    # ``test_verification_repository_round_blocked.py`` 逐条钉住。
    ":(exclude)backend/state_machine/repositories/verification_repository.py",
    ":(exclude)backend/state_machine/db/schema.py",
    ":(exclude)backend/state_machine/repositories/verification_repository.py",
    # 2026-11-19：本计划把验证状态的权威写从路由行的 `verification` 镜像列上
    # 移走（`write_plan_state` 不再写该列，该列只剩冻结基线且只有一个写者
    # `write_verification_baseline`），这条改动落在路由仓库上。改动只有这一列
    # 的归属，按本 gate 的既定流程给它一条窄排除，并把它逐字节钉在
    # `FROZEN_ROUTING_REPOSITORY_SURFACE_SHA256` 上。
    ":(exclude)backend/state_machine/repositories/routing_repository.py",
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


def scoped_exclusion_paths() -> list[str]:
    """The paths that ``FORBIDDEN_MODULE_PATHS`` excludes narrowly.

    A ``:(exclude)`` pathspec takes one file out of the change-surface
    diff. The pathspec says *what* is hidden; the prose next to the
    constant says *why*, and that is the half a reader needs.
    """
    return [
        path.split(":(exclude)", 1)[1]
        for path in FORBIDDEN_MODULE_PATHS
        if path.startswith(":(exclude)")
    ]


def exclusion_documentation() -> str:
    """The prose that documents ``FORBIDDEN_MODULE_PATHS``.

    The run of comment lines that immediately precedes the constant —
    the block that says why each narrow exclusion is there.
    """
    lines = Path(__file__).resolve().read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("FORBIDDEN_MODULE_PATHS:"):
            break
    else:  # pragma: no cover - the constant is in this file by definition
        raise AssertionError(
            "FORBIDDEN_MODULE_PATHS is not declared in this file"
        )
    start = index
    while start > 0 and (
        lines[start - 1].startswith("#") or not lines[start - 1].strip()
    ):
        start -= 1
    return "\n".join(lines[start:index])


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
#:
#: The set now carries ``playwright`` -- the browser the E2E lane drives
#: -- with its lockfile entry beside it (the manifest/lock agreement gate
#: holds).  The freeze moves to the branch's approved state: the
#: dependency was added on purpose, and the rule stays "the dependency
#: set changes once, and only that way".
FROZEN_REQUIREMENTS_HASH: str = (
    "57edf48275e05eb0eea55cbc9a5db2a946298df3eedaf854045d63a3271626eb"
)

#: The state db schema, frozen. A new table, a new column or a new
#: index changes this value.
#:
#: 2026-10-28 (native scheduler, schema v7): the value below was
#: re-pinned for the three scheduler tables — ``scheduler_queue``,
#: ``scheduler_slot``, ``scheduler_command`` — and their two indexes.
#: That is the whole delta: the previous baseline
#: (``bc821cabc9b8ec6d629a18aa4d036873478c66773353a97b5bf87e257750dd51``)
#: was computed against exactly the same ten objects the old schema
#: held, and the object list printed by the failing assertion shows
#: the three new tables and the two new indexes as the only additions.
#:
#: 2026-10-29 (scheduler panel, capacity override): re-pinned once
#: more.  The delta against the 2026-10-28 baseline is exactly one new
#: table, ``scheduler_setting`` — it holds the capacity override, which
#: the file configuration cannot hold because ``backend/config.yaml`` is
#: tracked and carries the comments that explain it.  The object list
#: printed by the failing assertion shows every other object unchanged,
#: so the previous baseline
#: (``4e31487b0ae462e470a54ecd03a5e7c6d1cdf5c90a61975c80519868535ddb23``)
#: is the hash of exactly the fourteen objects that preceded it, and the
#: new value is the hash of those same fourteen plus this one table.
#:
#: 2026-11-02 (victim priority): re-pinned once more.  The delta
#: against the 2026-10-29 baseline is exactly one **column** on one
#: existing table -- ``scheduler_slot`` gained ``priority INTEGER NOT
#: DEFAULT 0``, because the queue's priority row is deleted the moment
#: a plan is admitted and a running plan has to be readable for its
#: priority.  It is a column and not a table, so the object list
#: printed by the failing assertion is the same fifteen names as
#: before, unchanged in number and in name; only the stored ``CREATE``
#: text of ``scheduler_slot`` moved (SQLite rewrites that text when a
#: column is added, so the hash moves with it).
#:
#: The delta was checked, not assumed: deleting the one ``priority``
#: column line from the stored ``CREATE`` text of ``scheduler_slot``
#: reproduces the previous baseline
#: (``5ffab1223d67c64c0eb17147eaf479346b163d3dd8e6d0e19c31ddab71cfd521``)
#: byte for byte, so nothing else in the schema moved.
#:
#: 2026-11-04 (root-cause report delivery): re-pinned once more.  The
#: delta against the 2026-10-29 baseline is exactly two new columns on
#: ``plan_tasks`` — ``deliverable`` and ``report_path``.  They carry the
#: root-cause report's delivery channel: a report-style task's
#: deliverable is a file, and its address is in ``report_path``.  The
#: object list printed by the failing assertion shows every other object
#: unchanged, so the previous baseline
#: (``5ffab1223d67c64c0eb17147eaf479346b163d3dd8e6d0e19c31ddab71cfd521``)
#: is the hash of exactly the objects that preceded it, and the new value
#: is the hash of those same objects plus these two columns.
#:
#: Merging origin/main brings both deltas into one schema:
#: ``scheduler_slot.priority`` and ``plan_tasks.deliverable`` /
#: ``plan_tasks.report_path``.  The object list is therefore still the
#: same fifteen names -- three columns on two existing tables, no table
#: added and none removed -- and the frozen baseline moves to the hash of
#: that merged schema.  This is a move of the frozen baseline, not a
#: loosening of the gate: the assertion, the comparison and the object
#: list are all unchanged, and the schema stays pinned byte for byte.
FROZEN_SCHEMA_DDL_HASH: str = (
    "ce628e4a82fbeb707799f9935d26fed4ce53c824b6e93652f6c380c9fad98650"
)

#: The phase merge renamed the waiting bucket to ``queued``. The rename
#: reaches shared code — the frontend badge, its stylesheet and the
#: verification loop — so the change-surface gate has to be told about
#: it. These three files are excluded from that diff and frozen here
#: instead, byte for byte: the diff gate stops reporting the one
#: approved change, and this gate stops reporting any *other* change
#: to the same files.
#:
#: The exclusion is not a blank cheque. Every byte of these files is
#: pinned to the hash below, so a later edit to any of them fails this
#: gate even though it no longer shows up in the diff.
#:
#: ``backend/verification_loop.py`` carries the **capacity-wait**
#: work as well as the rename: the park (and the crash verdict that
#: must not read as a park) lives in the round's own loop, and the
#: freeze is moved to the branch's approved state so that the rule
#: stays "these files change once, and only that way" — the approved
#: change being the rename plus the capacity work, not the rename
#: alone.
#:
#: 2026-10-29: the same file now also carries the **base-drift
#: hand-off** — a round whose base-drift preflight found conflicts
#: never dispatched a verification point, and the loop has to
#: recognise that round instead of reading its empty report as "every
#: verification point failed".  The change is additive (+121 lines:
#: one import, one constant pair, the handler and its call site); no
#: phase name, no park state and no existing branch of the loop
#: changed, which is why the freeze moves rather than the gate being
#: loosened.
#:
#: ``frontend/app.js`` carries one more approved change on top of the
#: rename: the legacy ``esc()`` helper now coerces its argument to a
#: string before escaping it.  A task id in ``tasks.json`` is allowed to
#: be a **number**, and ``(1).replace`` is a ``TypeError`` -- the page
#: caught it and painted the empty state over a plan that *had* tasks,
#: silently.  On 2026-10-29 the native scheduler's SPA view merged into
#: the same file (its renderers), so the pin moves once more to the
#: merged content.
#:
#: 2026-10-29 / 2026-10-30 (scheduler panel): the file moved once more
#: after that — the scheduler view was re-laid out, the running column
#: gained the real elapsed time, the capacity area gained the parallel
#: agent count and the capacity dropdown.  The pin moves to the branch's
#: approved state for the same reason it moved for the capacity work:
#: the file changed once, on purpose, and the rule stays "these files
#: change once, and only that way".
#:
#: The scheduler panel's own refresh work follows it there: the panel
#: re-reads the read model on a timer, and a hidden page stops that
#: timer until the operator comes back -- so the pin moves once more to
#: that content.  The poll's own failure handling moves with it: a
#: quiet read that fails keeps the last good frame on screen and says
#: what went wrong in the console, and the same failure is written only
#: once -- so the pin moves once more to that content as well.
#:
#: 2026-10-31: the poll's failure handling is corrected once more, and
#: the pin moves with it.  What makes two failures the same failure is
#: the **read** *and* the message -- the key is ``(source, message)`` --
#: and the marker that every read shared is gone.  The shared marker
#: could not tell one read from another, and the reads go through the
#: same ``api()`` wrapper: a backend that is down made them fail with
#: the very same message, so the second failure was swallowed by the
#: first one's marker and the console showed one line for two broken
#: reads.  The memory is now keyed by the read
#: (``_schedulerPollErrors[source]``), and a read that succeeds again
#: forgets only its own failure -- another read may still be failing
#: with the same message, and that failure must not be written a
#: second time on the next tick.  The quiet/non-quiet behaviour, the
#: poll's interval and the timer's visibility handling are untouched,
#: so the pin moves once more to that content.
#:
#: 2026-10-31 (merge with origin/main, PR #70): the 「可操作的计划」
#: block is gone from the panel, and with it ``loadSchedulerPlans()``
#: and ``renderSchedulerPlans()`` -- the panel now reads the read model
#: and the parallel-agent count, and the poll reads the read model
#: alone.  The file changed once, on purpose, for the same reason it
#: moved for the capacity work, so the pin moves again.
#: 2026-10-31: the same file now carries the **failed-phase action
#: split** — the scheduler panel offers 「启动」 and 「重跑验证」 as two
#: separate buttons, because they are two different commands: 「启动」
#: puts a failed plan back in the queue, while 「重跑验证」 re-runs the
#: verification round (two requests: the round counter is rewound, then
#: the round starts).  The two actions need two different words, so the
#: button's label is now chosen per *phase* and not per action — on a
#: failed plan 「启动」 would read as "run the whole thing again" while
#: it only re-queues, so that phase says 「重新排队执行」 instead.
#: The change is additive (+66/-5 lines: one action label, one
#: phase-label table, one label helper, one re-run function and its
#: dispatch arm, and the phase argument on the button renderer); no
#: existing phase name, no existing action and no existing branch of
#: the renderer changed, which is why the freeze moves rather than the
#: gate being loosened.
#:
#: 2026-10-31 (合入 main): 两侧的改动都在这个文件里 —— main 的调度面板改版
#: （视图重排、在跑运行时长、并行 Agent 数、容量下拉框）与本分支的 failed 相位
#: 动作拆分（两个按钮、failed 相位专属文案、重跑验证的两次请求），以及为终态
#: 计划区块保留的 renderer。哈希按本 gate 的既定流程重新钉住合并后的文件。
FROZEN_R10_SURFACE_SHA256: dict[str, str] = {
    # 2026-10-29：因「调度面板自动刷新」新增 5 秒轮询与「页面不可见时暂停、回可见立即刷新」
    # （`refreshScheduler` / `handleSchedulerVisibilityChange`），按本 gate 的既定流程重新钉住
    # 该文件的哈希。
    # 同日 task-3 的「失败保留上一帧、同类失败只记一条」（`schedulerLogPollError` /
    # `schedulerClearPollError`）也属于本次已批准的调度轮询改动，随同一份内容一并前移重钉。
    # 2026-10-31：VP-020 把「同类失败」的判据钉成「按 (source, message) 去重、成功只清该
    # source 的标记、跨 source 各自记录」，实现随之改为以 source 为键的记忆
    # （`_schedulerPollErrors[source]`）并删掉所有读共享的标记；quiet/非 quiet 分支、
    # 轮询间隔与可见性处理一字未动，按本 gate 的既定流程把该文件的哈希再前移一次。
    # 2026-10-31：合入 origin/main（PR #70）并删除已被 main 删掉的 `loadSchedulerPlans` /
    # `renderSchedulerPlans` 之后，文件内容再次变化，按本 gate 的既定流程再钉一次。
    # 2026-10-31：因 failed 相位拆分「启动」与「重跑验证」两个动作（新增 rerun-verification
    # 动作、failed 相位专属文案「重新排队执行」、重跑验证的两次请求）与调度面板改版
    # （视图重排、在跑运行时长、并行 Agent 数、容量下拉框），按本 gate 的既定流程
    # 重新钉住该文件的哈希。
    # 2026-11-02：因「点击计划卡片必须进入『循环停止』验证面板」这条要求改了
    # `resumePlan` 的相位分派 —— `failed` / `stopped` 这两个执行终态现在走
    # `showTasksView`（验证面板带着停止原因、轮次计数、结果与修复清单），
    # 而不是落回访谈页把一个已经写好结论的需求重新问一遍。该要求对应
    # verification convergence PRD 的「终态计划要能打开它自己的结论」一条，
    # 也正是 repair-r1-02 修的那个场景。按本 gate 的既定流程重新钉住该文件的哈希。
    # 2026-11-01：因「调度卡片按卡片渲染、默认收起、展开才请求详情」（task-12）改动了
    # `frontend/app.js`，按本 gate 的既定流程把哈希前移到本分支的批准态。表内条目集合
    # 与断言强度均不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-14（本次合入 origin/main）：两侧的改动落在同一文件的**不同位置**：
    # main 的「容量下拉框不得自己跳数」与本分支的「调度卡片按卡片渲染、默认收起、
    # 展开才请求详情」互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的
    # 既定流程把冻结基线前移到**合并态**哈希。表内条目集合与断言强度不变：被钉文件
    # 的范围不变、sha256 仍逐字节比对。
    # 2026-11-01：因已批准的 VP-009 修复（f623224e「加载期的 503 不再变成未捕获的
    # Promise rejection，取证副本搬出被服务的 plans 树」）给该文件加了一层加载期的
    # 拒绝处理（`loadInBackground` / `renderPlansError` / `renderSidebarError`），
    # 按本 gate 的既定流程把冻结基线前移到该批准态的哈希。
    # 前移之前已核对：自上一版基线（bcab67d5 合并 origin/main 之后的状态）以来，
    # 该文件只有 f623224e 这一处改动（+95/-18），没有夹带别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-04：合入 origin/main —— 两侧的改动落在同一个文件上，但改的是**不同的
    # 位置**：main 的 VP-009 加载期拒绝处理（`loadInBackground` / `renderPlansError` /
    # `renderSidebarError`）与本分支的「调度卡片按卡片渲染、默认收起、展开才请求详情」
    # 互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的既定流程重新钉住
    # 合并后的文件。
    # 2026-11-05：因「容量下拉框未应用的选择不被轮询抹掉」给该文件加了一层
    # 「未应用的选择」状态（`select.dataset.pending` 的置位与清除、
    # `renderSchedulerCapacity` 里受保护的 `select.value` 赋值），按本 gate 的既定流程
    # 重新钉住该文件的哈希。这是「移动冻结」而不是「放松门禁」：断言强度、被钉文件的
    # 范围与逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-07：合入 origin/main（2fe98191）—— 两侧的改动仍然落在同一个文件上，
    # 改的还是**不同的位置**：main 的「未应用的选择」状态与本分支的「调度卡片按卡片
    # 渲染、默认收起、展开才请求详情」互不覆盖，故两边都保留；文件内容因此再次
    # 变化，按本 gate 的既定流程重新钉住合并后的文件。表内条目集合与断言强度不变：
    # 被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-09：R4「容量下拉框不得自己跳数」的其余两处已批准改动也落在本文件 ——
    # 43bec418（2026-11-08）「切走视图时结束容量草稿并恢复服务端数字」（新增
    # `schedulerClearCapacityDraft`，`goHome` 切走视图时调用它把控件放回服务端数字）
    # 与 448d67df（2026-11-08）「应用后以服务端应答的数字为准」
    # （`schedulerApplyCapacity` 改以应答里的 `capacity.total` 为准，数字被规整时显示
    # 「服务端规整为 N」的提示；另加 `schedulerStateSeq`，过期读不覆盖新写）。
    # 连同 2026-11-05 已钉的 09fcb663（轮询期间保留未应用的选择），本文件在这一条已
    # 批准的需求上的改动就是这三笔，没有夹带别的改动，按本 gate 的既定流程把冻结基线
    # 前移到本分支的批准态。
    # 前移之前已核对：自上一版基线（09fcb663，即 2026-11-05 钉住的状态）以来，
    # 该文件只有 43bec418 与 448d67df 两处改动（+129/-7），没有夹带别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-14：合入 origin/main（316b360a）—— 两侧的改动落在同一文件的
    # **不同位置**：main 的「容量下拉框不得自己跳数」（`schedulerClearCapacityDraft`
    # / `schedulerApplyCapacity` / `#scheduler-capacity-notice`）与本分支的「调度
    # 卡片按卡片渲染、默认收起、展开才请求详情」互不覆盖，故两边都保留；文件
    # 内容因此再次变化，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-14（本次合并 origin/main@ecc5d9ee，13 个新提交）：本分支自合并基线
    # `33041be4` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `51ad1ae1…`），改它的只有 base 侧（调度卡片的详情缓存与轮询）。
    # 因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 是 `5887449c…`
    # （即 ecc5d9ee 那次合并后所钉的值），按本 gate 的既定流程把冻结基线前移到
    # **合并态**哈希。
    # 前移的两端都记在这里：基线由 `51ad1ae1…`（合并前的批准态）移到
    # `5887449c…`（合并后的批准态）。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 仍逐字节比对。
    # 2026-11-15：本计划 PRD（import.json）需求二「内容（与飞书卡片对齐）」与需求一
    # 「交互稳定性」的已批准改动落在本文件：041a1f9c（task-6）「按 sections 渲染详情并
    # 保留 lines 回退」（需求二）、8a6e31d2（task-7）「详情过期只刷新不清空」、
    # 7e2bbdab（task-8）「按 plan_id 复用卡片 DOM」（后两者属需求一）。
    # 前移之前已核对：自上一版基线（e8b568f4，即 2026-11-14 钉住的状态）以来，
    # 该文件只有上述三处已批准的改动，没有夹带别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-16（本次合并 origin/main@78ffdddc，7 个新提交）：本分支自合并基线
    # `67531555` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `5887449c…`），改它的只有 base 侧（上面 2026-11-15 那三处已批准
    # 改动）。因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 就是
    # 下方所钉的 `b8c6970f…`，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `5887449c…`（合并前的批准态）移到
    # `b8c6970f…`（合并后的批准态）。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 仍逐字节比对。
#:
    # 2026-11-17（本次合入 origin/main@78ffdddc）：base 侧的「调度面板卡片原地更新
    # 并展示完整进度分节」（PR #166 —— `schedulerCardFromHtml` /
    # `schedulerUpdateCard` 与进度分节渲染）与本分支的「终态计划走验证面板」
    # （`resumePlan` 的 `failed` / `stopped` 相位分派）落在**不同位置**，互不覆盖，
    # 故两边都保留；文件内容因此再次变化，按本 gate 的既定流程把冻结基线前移到
    # **合并态**哈希。表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍
    # 逐字节比对。
    # 2026-11-16：本分支已批准的改动进入冻结面 —— task-23「调度页新增等待区块与
    # 要求书编辑入口」（4dc2f275）在调度面板加了等待区块与延长/终止/回答三个操作
    # （`renderSchedulerWaiting` / `schedulerExtendWait` / `schedulerTerminateWait` /
    # `schedulerAnswerWait`），task-repair-01 的合并解冲突（b8a421f1）又落在同一
    # 文件的同一处，两侧的意图都保留；文件内容因此再次变化，按本 gate 的既定流程
    # 把冻结基线前移到**合并态**哈希。哈希取自工作区文件的 `shasum -a 256` 实测值，
    # 不是抄来的。表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节
    # 比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-16（合入 origin/main 78ffdddc / PR #168）：main 侧对同一文件也有已批准
    # 改动 —— 041a1f9c（task-6）「按 sections 渲染详情并保留 lines 回退」（需求二）、
    # 8a6e31d2（task-7）「详情过期只刷新不清空」、7e2bbdab（task-8）「按 plan_id 复用
    # 卡片 DOM」（后两者属需求一）。两侧改动落在同一文件的**不同位置**，自动合并无冲突，
    # 两侧的意图都保留；文件内容因此再次变化，按本 gate 的既定流程把冻结基线前移到
    # **合并态**哈希（`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的
    # 范围、sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-17（本次合并 origin/main@b9b9061b，4 个新提交）：本分支自合并基线
    # `78ffdddc` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `b8c6970f…`），改它的只有 base 侧（收尾阶段的等待区块与错误渲染：
    # `renderSchedulerWaiting` / `renderSchedulerError`，+376/-2）。
    # 因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 就是下方所钉的
    # `86c333a6…`，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `b8c6970f…`（合并前的批准态）移到
    # `86c333a6…`（合并后的批准态）。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 逐字节比对的强度不变。
#:
    # 2026-11-17（本次合入 origin/main@b9b9061b —— PR #170「验证通过后的收尾阶段」）：
    # base 侧在 78ffdddc 之后又给调度面板接上了收尾阶段的等待区块与要求书编辑入口
    # （`renderSchedulerWaiting` / `schedulerExtendWait` / `schedulerTerminateWait` /
    # `schedulerAnswerWait` / `loadPostprocessSpec` / `savePostprocessSpec`），与本分支
    # 的「终态计划走验证面板」（`resumePlan` 的 `failed` / `stopped` 相位分派）落在
    # **不同位置**，互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的既定流程
    # 把冻结基线前移到**合并态**哈希（`shasum -a 256` 实测值）。表内条目集合与断言强度
    # 不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-19：因已批准的「侧栏计划状态词表统一并自动刷新」改动了 `frontend/app.js`
    # —— `phaseLabel` / `phaseClass` 补上 running / failed / completed / stopped /
    # verification / conflict / archived 七个执行相位，并新增侧栏轮询
    # （`SIDEBAR_POLL_MS` / `startSidebarPolling` / `stopSidebarPolling` /
    # `refreshSidebar` / `handleSidebarVisibilityChange`），按本 gate 的既定流程把
    # 冻结基线前移到本分支的批准态。哈希取自工作区文件的 `shasum -a 256` 实测值，
    # 不是抄来的。表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节
    # 比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-21：因已批准的「停止链的徽章跟随后端状态机」改动了 `frontend/app.js`
    # —— `setExecBadge` / `setVerificationBadge` 的词表补上 `stopping`（正在停止），
    # `execBadgeState` 让 `stopping` 先于 `paused`，`stopExecution()` 读停止应答：
    # 应答是 `stopping` 就显示「正在停止」并继续轮询，只有轮询到终态才停轮询并显示
    # 「已停止」，`startExecutionPolling()` 在停止窗口内不把徽章刷回「执行中」，
    # `resumePlan()` 的相位分派补上 `user_stopping`（正在停止的计划仍能打开任务视图）。
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态。哈希取自工作区文件的
    # `hashlib.sha256(Path("frontend/app.js").read_bytes()).hexdigest()` 实测值，
    # 不是抄来的。表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节
    # 比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-23：因本次「调度页收尾分区 + 调度视图 URL + 删掉要求书编辑框」改动
    # `frontend/app.js`（新增 `loadSchedulerFinishing` / `renderSchedulerFinishing`
    # 与 `schedulerSyncUrl` / `handleSchedulerHashChange`，抽出 `schedulerWaitingActions`
    # 共用函数，删掉要求书那一段），按本 gate 的既定流程把冻结基线前移到本分支的
    # 批准态。哈希取自工作区文件的 `shasum -a 256` 实测值，不是抄来的。表内条目
    # 集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    "frontend/app.js": (
        "c8ffe24d9ef057cec9b863d6ce135b28d5d22a47e6766d0b95923b3136eda094"
    ),
    # 2026-11-01：因「调度卡片容器与收起态样式」（task-11）改动了 `frontend/style.css`，
    # 按本 gate 的既定流程把哈希前移到本分支的批准态；表内条目集合不变。
    # 2026-11-14（本次合并 origin/main@ecc5d9ee）：本分支自合并基线 `33041be4`
    # 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同（sha256
    # `0cab7b66…`），改它的只有 base 侧（调度卡片容器的样式）。因此合并后本文件
    # 的内容与 base 侧逐字节相同，其真实 sha256 是 `2f1a86a3…`（即 ecc5d9ee 那次
    # 合并后所钉的值），按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `0cab7b66…`（合并前的批准态）移到
    # `2f1a86a3…`（合并后的批准态）。
    # 表内条目集合不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-15：本计划 PRD（import.json）需求三「样式」的已批准改动落在本文件：
    # fb965ed6（task-9）「分区 / 进度条 / 任务行样式」（分区标题、等宽进度条、任务行
    # 状态图标、次要信息弱化色）。前移之前已核对：自上一版基线（e8b568f4，即 2026-11-14
    # 钉住的状态）以来，该文件只有这一处已批准的改动，没有夹带别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-16（本次合并 origin/main@78ffdddc，7 个新提交）：本分支自合并基线
    # `67531555` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `2f1a86a3…`），改它的只有 base 侧（上面 2026-11-15 那处已批准改动）。
    # 因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 就是下方所钉的
    # `d77285b4…`，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `2f1a86a3…`（合并前的批准态）移到
    # `d77285b4…`（合并后的批准态）。
    # 表内条目集合不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-16：task-23「调度页新增等待区块与要求书编辑入口」（4dc2f275）给该文件
    # 加了等待表格与要求书编辑区的样式，task-repair-01 的合并解冲突（b8a421f1）又
    # 落在同一文件，两侧的意图都保留；按本 gate 的既定流程把冻结基线前移到**合并态**
    # 哈希（`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的范围
    # 不变、sha256 仍逐字节比对。
    # 2026-11-16（合入 origin/main 78ffdddc / PR #168）：main 侧对同一文件也有已批准
    # 改动 —— fb965ed6（task-9）「分区 / 进度条 / 任务行样式」（分区标题、等宽进度条、
    # 任务行状态图标、次要信息弱化色）。两侧改动落在同一文件的**不同位置**，自动合并
    # 无冲突，两侧的意图都保留；按本 gate 的既定流程把冻结基线前移到**合并态**哈希
    # （`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 逐字节比对的强度不变。
    # 2026-11-17（本次合并 origin/main@b9b9061b，4 个新提交）：本分支自合并基线
    # `78ffdddc` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `d77285b4…`），改它的只有 base 侧（等待表格与要求书编辑区的样式：
    # `.scheduler-waiting-*` / `.scheduler-spec-*`，+98/-0）。
    # 因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 就是下方所钉的
    # `7703bc85…`，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `d77285b4…`（合并前的批准态）移到
    # `7703bc85…`（合并后的批准态）。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 逐字节比对的强度不变。
#:
    # 2026-11-17（本次合入 origin/main@b9b9061b —— PR #170「验证通过后的收尾阶段」）：
    # 该文件只被 base 侧改动（本分支未动它），合并后的内容与 base 侧一致；按本 gate
    # 的既定流程把冻结基线钉在**合并态**哈希（`shasum -a 256` 实测值）。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 逐字节比对。
    # 2026-11-19：因已批准的「侧栏计划状态词表统一并自动刷新」给该文件加了七个执行
    # 相位的配色规则（`.phase-running` / `.phase-failed` / `.phase-completed` /
    # `.phase-stopped` / `.phase-verification` / `.phase-conflict` /
    # `.phase-archived`），按本 gate 的既定流程把冻结基线前移到本分支的批准态。
    # 哈希取自工作区文件的 `shasum -a 256` 实测值，不是抄来的。表内条目集合与断言
    # 强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-23：因本次改动删掉了要求书编辑框的 `.scheduler-spec-*` 样式、并新增
    # 收尾卡片的 `.scheduler-finishing-*` 样式，改动了 `frontend/style.css`，
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态。哈希取自工作区文件的
    # `shasum -a 256` 实测值，不是抄来的。表内条目集合不变。
    "frontend/style.css": (
        "8f867c72aa08ca46bd0db2b56e230cd3a27bea9078d6b0e41f1ee306179c2c36"
    ),
    # 2026-10-31：因本分支新增 `_repair_exit_writer`（修复轮退出事件的单一写入器）
    # 改动本文件，按本 gate 的既定流程重新钉住该文件的哈希。
    # 2026-10-29：因「关机检查点恢复」新增 `_requeue_shutdown_interrupted_verification`（+80 行），
    # 按本 gate 的既定流程重新钉住该文件的哈希。
    # 2026-10-31：因「基线漂移冲突转可执行合并任务」把合并场景名经 `PDT_EXECUTOR_SCENE`
    # 跨进程传给 executor 子进程（executor 是子进程，与父进程不共享 Python 状态，故只能走
    # 环境变量），改动为纯增量：一个形参、一个 `extra_env` 字典、一处调用点实参（+30 行），
    # 未改动任何既有分支；按本 gate 的既定流程把冻结基线移动到本分支的批准态哈希。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-01（本分支）：本次前移只涉及三个前端文件，本分支未改动
    # `backend/verification_loop.py`。
    # 2026-11-01：合入 origin/main（PR #74）—— 两侧的改动都落在同一个文件的同一处
    # spawn 调用上，但改的是**不同的形参**：main 的「基线漂移合并场景经
    # `PDT_EXECUTOR_SCENE` 跨进程传给 executor 子进程」加的是 `scene` 形参与
    # `extra_env` 实参；本分支的「修复轮退出事件的单一写入器 `_repair_exit_writer` +
    # 回收器按 `repair_executor` 角色分序回收」加的是 `role` 实参。两者互不覆盖，
    # 故两边都保留；文件内容因此再次变化，按本 gate 的既定流程重新钉住合并后的文件。
    # 2026-11-02：因本轮修复的两条要求改动本文件，按本 gate 的既定流程重新钉住哈希：
    #   * §3「没测出东西的轮次不占编号」—— `_round_release_reason` /
    #     `_release_unverified_round` / `_round_verification_entries`；
    #   * 「关停不写成崩溃」—— 关停时 `process.wait()` 返回的 -15 是服务器自己
    #     发的信号，不能写成 `process_died_unexpectedly` / `repair_non_zero_exit`，
    #     否则关停检查点写下的 `queued` 会被终局判定覆盖，计划的结论就永远停在
    #     终态、再也不会被捡起来（`_mark_failed_dead` / `_lazy_check_execution` /
    #     修复轮 watcher 三处都改为「只记录、不判定」）。
    # 同时把 §3 注释里的真实计划号换成脱敏占位（真实计划号是私有信息，不属于源码）。
    # 2026-11-02（repair-r2-01-2 收口）：因上一条里的「关停不写成崩溃」还有残余再改本
    # 文件 —— 修复轮 watcher 的关停分支只压住了判定，却仍然回调 `on_complete`：那个
    # 回调是 `_on_repair_complete`，它 `begin_verification()` 写下相位并起一轮新的
    # 验证。关停时这是给一台正在退场的服务器开新活，而且它写下的相位与检查点要落的
    # `queued` 相冲（检查点说「被打断」，相位说「已收尾」，检查点就被覆盖）。现在回调
    # 与判定一起让位：关停打断的这一轮没有自己的结论，结论归下一次启动。同一函数的
    # 兜底分支（watcher 自身抛异常）原本无条件写 `failed` /
    # `process_died_unexpectedly`，关停期间同样会覆盖检查点，现在兜底与判定走同一条
    # 规则（真正的崩溃照旧判定）。
    # 这一条是上面「关停不写成崩溃」的残余（根因二），不是 PRD 新增的一条要求：被打断
    # 的轮次同样没有结论，所以除了不该留下终局判定，也不该再起一轮 —— 而重新起的那一
    # 轮会吃掉一个轮次编号，正好撞上 verification convergence PRD §3「没测任何东西的
    # 轮次不消耗轮次」。按本 gate 的既定流程重新钉住该文件的哈希。
    # 2026-11-01（合入 PR #75 / base）：base 侧对 `backend/verification_loop.py` 的改动
    # （`_repair_exit_writer` + 回收器按 `repair_executor` 角色分序回收）已随 base 合入，
    # 合并后该文件的内容与本条哈希一致，故本条哈希在本次合并后保持不变。
    # 2026-11-14（本次合入 origin/main）：本分支的已批准改动（§3 的轮次释放、关停不
    # 写成崩溃等）与 base 侧的「轮次实例身份」改动落在同一文件的不同位置，互不覆盖，
    # 故两边都保留；文件内容因此再次变化，按本 gate 的既定流程把冻结基线前移到
    # **合并态**哈希。表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍
    # 逐字节比对。
    # 2026-11-03：本分支修「验证被判死后那一轮没有真正停下」——判死时按轮次的
    # token 收掉这一轮拥有的全部 coding-tool 子进程、强制释放轮次租约、让僵尸
    # 轮次写终态被拒、并给长 LLM 调用加进度行与墙上限。四处改动都落在本文件
    # （`_cleanup_round_processes` / `_force_release_round_lease` / 终态拒绝 /
    # 查询心跳），未改动任何既有分支的语义；按本 gate 的既定流程重新钉住该文件。
    # 2026-11-03：修「配额 park 只活在内存里」——`_park_repair_for_quota` 的幂等
    # 判据从「内存 status」改为「磁盘相位」，让唯一落库者
    # `routes.execution._enter_quota_wait` 重新可达；同时把「配额暂停的 repair 出口」
    # 归到 `waiting_for_quota` 且不再给已 park 的轮次写 `ended_at`。改动只落在本文件
    # 的 repair 出口与 park 判据，按本 gate 的既定流程重新钉住该文件。
    # 2026-11-04：合入 origin/main 后，本条哈希与合并后文件的真实 sha256 一致，
    # 保持不变。
    # 2026-11-04：preflight 被 binary freshness 挡下的轮次不再进入判定与修复链
    # —— 新增 `_binary_freshness_blocked_report` 与对应的 `_record_terminal(
    # "loop_stopped", BINARY_FRESHNESS_PREFLIGHT_ERROR_KIND)` 出口，插在
    # service preflight 之后、base drift 之前；该轮不计入计划的 round 预算。
    # 改动只落在本文件的 preflight 分支链，按本 gate 的既定流程重新钉住该文件。
    # 2026-11-06：验证轮次作用域与根因链预检——本文件新增轮次开头记录 `head_sha`
    # 与变更集、复用 verdict 以本轮 round 重新落盘并标 `reused_from_round`、
    # 关卡只读本轮结论，以及 `start_verification_cycle` 进入验证轮之前的链预检
    # （预检派发不推进 round）。改动只落在轮次记录与预检接线，未改动既有分支语义；
    # 按本 gate 的既定流程重新钉住该文件。这是「移动冻结」而不是「放松门禁」：
    # 断言强度、被钉文件的范围与逐字节比对的强度都不变，该文件此后仍被逐字节钉住。
    # 2026-11-06（续）：根因链整条删除——本文件删掉 `_handle_root_cause_preflight`
    # 与其调用链（链的预检不再存在），其余轮次逻辑一字未动；按本 gate 的既定流程
    # 把冻结基线前移到删除后的批准态。这是「移动冻结」而不是「放松门禁」。
    # 2026-11-07：合入 origin/main（2fe98191）后，本条哈希与合并后文件的真实
    # sha256 仍然一致（两侧都未再改这个文件），保持不变。
    # 2026-11-08：修「确认修复时相位不是 verification_repairing 导致验证轮崩溃」
    # ——本文件**只改注释**：把「覆盖 status 就会让 orchestrator 的
    # `verification_failed` → `start_verification_repair` 路径跑起来」这句
    # 与事实不符的说明改成真相（覆盖只让本函数走修复出口，相位由
    # `confirm_repair_and_rerun` → `ensure_verification_repairing` 建立），
    # 并在空修复队列的分支补上「`check_cycle_conditions` 只在普通失败分支
    # 才会把计划放进 `verification_repairing`」这一限定。一行可执行代码都
    # 未改动；按本 gate 的既定流程重新钉住该文件。这是「移动冻结」而不是
    # 「放松门禁」：断言（mismatched == []）、被钉文件的范围与 sha256 逐字节
    # 比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-08：合入 origin/main（ba07388e，含 #119/#120/#123）—— 两侧的改动
    # 落在**不同的文件**上：main 侧改了 `backend/verification_loop.py`（#123 的
    # 注释修正）与 `backend/state_machine/repositories/execution_repository.py`
    # （#120 的 `update_status` 可选 `stop_reason`），本分支只改了三个前端文件，
    # 两边互不覆盖，故都保留。合并后 `backend/verification_loop.py` 的内容与
    # main 侧逐字节相同，其真实 sha256 与下方所钉的值一致，故本条哈希在本次
    # 合并后保持不变。这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、
    # 被钉文件的范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-09：本计划任务 2 删除了 `plan_routing.verification` 的镜像写函数
    # `_update_plan_state_to_terminal` 及其调用点（镜像列不再是验证状态的生产
    # 写入点，权威行取代它）。
    # 前移之前已核对：`git diff main -- backend/verification_loop.py` 只有这
    # 一处已批准的改动（+2/-169 —— 2 行是注释「Default; the branch inside the
    # try narrows it to the caller's verdict.」，其余为镜像写函数整段删除），
    # 没有夹带别的改动。
    # 前移的两端都记在这里：基线由
    # 3fa8684b1ed4c2f9bdf0b9a27daf46c0c2df9310e07928bf91210bb84f4ab3e0
    # （改动前的批准态）移到
    # 4123ac1df0822c0fcd8f107f1bfefd2da31656bfaf4780009051fc7453f3252e
    # （改动后的批准态）。前移这一步本身可被复核，不必再回溯 git 历史才能
    # 确认「移动」是从哪个基线移到了哪个基线。
    # 按本 gate 的既定流程把冻结基线前移到该批准态的哈希。这是「移动冻结」
    # 而不是「放松门禁」：断言（mismatched == []）、被钉文件的集合
    # （frontend/app.js / frontend/style.css / backend/verification_loop.py）
    # 与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑
    # 照样红。
    # 2026-11-09：本计划 task-7「修复轮的同步/异步路径也写兜底现场」已批准，
    # 改动本文件——新增 `_repair_stop_reason`（正数退出码写成
    # `repair_non_zero_exit:3`，负数信号写成 `repair_signal:9`），
    # `_repair_exit_writer` 在修复轮以非零码结束时补写崩溃现场（与主执行路径
    # 的父进程兜底报告同一套写入器），`_run_repair_execution_async` 改用新助手
    # 写 `stop_reason`。改动范围是相对上一版冻结基线（144d07b8）的 +80/-1、
    # 四处 hunk，全部落在本文件，未改动任何既有分支的语义；按本 gate 的既定
    # 流程把冻结基线前移到本分支的批准态哈希。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-10：「查询墙钟上限与判死击杀各收敛到一个点」这一已批准改动修改了
    # `verification_loop.py`——删掉了 `_apply_verification_query_cap` 及其两个调用点
    # （修复轮构造工具处、主循环调度处），常量 `VERIFICATION_LLM_QUERY_CAP_SECONDS`
    # 与 `VERIFICATION_LLM_QUERY_CAP_ENV` 改为从 `coding_tool` 重新导出；
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-10：「交棒不得被判成线程死亡」这一已批准改动修改了
    # `verification_loop.py`——死亡阶梯的 `not thread_alive` 分支先问
    # `_plan_execution_in_flight(plan_id)`（交棒时返回 True → 记 INFO 后直接返回，
    # 不判死；读记录抛异常按「不是交棒」处理），新鲜度 glob 集合加入
    # `logs/repair_*.log`（修复轮在另一个进程里跑，进度写在那个文件里）。
    # 改动全部落在本文件，相对上一版冻结基线（7b4385a3…）为 +54/-0，
    # 没有夹带别的改动；按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-11：本分支已批准的改动落在 `verification_loop.py` 上，进入冻结面的
    # 关键新增有三处 ——
    #   * 任务 1「验证轮判决落盘」新增 `_ROUND_VERDICT_STATUS_ALIASES`：把
    #     orchestrator 自己的拼写 `verification_failed` 映射成列本来就说的
    #     `failed`（判决表此前只认 `passed` / `failed` / `loop_stopped`，别的拼写
    #     被静默丢弃，轮次的判决就没落盘，计划的行一直停在 `running`）。
    #   * 任务 2「看门狗与启动期：内存/DB 分叉状态收敛」新增
    #     `_VERIFICATION_DIVERGENT_MEMORY_STATUSES`：`verification_failed` 与
    #     `failed` 两个拼写都表示「这一轮已经有判决」，内存里的状态与盘上的行
    #     分叉时按这个集合判，而不是按某一个字符串。
    #   * 任务 8「看门狗分叉收敛：排除 repairing 相位」给 `_divergence_gates_pass`
    #     追加第 4 道门：相位先经 `plan_state.normalize_phase` 归一化，再与
    #     `verification_repairing` 比较（历史别名 `verification_rerunning` 一并
    #     排除），正在合法续跑的计划不被误收敛为终态。
    # 与此同时，任务 6「陈旧检测：进度心跳不能无限期掩盖卡死」把心跳的计量搬进
    # 本文件（`_PROGRESS_HEARTBEAT_EVENT` / `_execution_log_liveness_ts` /
    # `_execution_log_evidence_ts`），本任务「心跳的计量锚点按轮次最后一次真实活动
    # 计」又把它从「轮次起始」改成「轮次最后一次真实活动」
    # （`_ROUND_ACTIVITY_GLOBS` 的最新 mtime 与 `execution.log` 最后一条非进度事件
    # 取 max，再与轮次起始取 max）。
    # 这些新增都只是**复用既有拼写**：`failed` / `verification_failed` /
    # `verification_repairing` / `coding_query_progress` 这几个词在本文件里本来
    # 就，没有引入任何新的相位词汇；比较方式也一律是「先归一化，再比较」。
    # 合并 origin/main 时同时带入 PR #143「构造 PlanState 不再写库」：自动验证
    # 回路的「让行存在」一处改为 `PlanState(plan_dir).ensure_row()`（构造函数
    # 不再有建行副作用）。两侧改动合并后，冻结基线前移到合并态哈希。
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希 —— 先例见 PR #138
    # （`fix/watchdog-handoff-not-death`）的「移动冻结」：那一次同样是
    # `verification_loop.py` 的已批准改动进入冻结面，走的正是这一条流程。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-13：合并 origin/main 时 ``backend/verification_loop.py`` 两侧都改过
    # ——本分支删掉了镜像写函数 ``_update_plan_state_to_terminal`` 及其调用点
    # （镜像列不再是验证状态的生产写入点，权威行取代它），main 侧则带来了
    # 2026-11-09 至 2026-11-11 的若干已批准改动。合并后的文件内容与两侧的
    # 冻结基线都不同，因此按本 gate 的既定流程把冻结基线前移到合并态哈希。
    # 前移之前已核对：合并后该文件里本分支的镜像写函数与其三个调用点仍然
    # 一并消失（``server.py`` 的导入、``verification_loop.py`` 的两处调用、
    # ``routes/verification.py`` 的 ``user_force_terminal`` 一处），改由
    # ``_persist_verification_terminal`` 写权威行；两侧的改动没有互相抵消。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑
    # 照样红。
    # 2026-11-13：本分支已批准的「轮次实例身份」改动落在 `verification_loop.py` 上 ——
    #   * 新增按**实例**铸造的 round token `_new_round_instance_token()`：token 形如
    #     `verification_loop-<pid>-<uuid>`（带 pid 是为了让崩溃后遗留的注册表能被认出
    #     已陈旧），**不含轮次号** —— 一个实例是另一个实例，不是「下一轮」；
    #   * 新增 `_claim_round_instance_ownership()` 的**条件抢占**：无人持有 → 取；
    #     本轮已持有 → 幂等地再取；别人持有且**仍然活着**（工作流租约还在）→ 让位
    #     并记 WARNING（活着的轮次保留该角色）；别人持有但已经结束（租约没了或过期）
    #     → 接管。存活与否按工作流租约判定，不按 owner 注册表判定；
    #   * 新增 `_release_handover_service_owner()`：交棒的那一轮在派完 executor 之后
    #     就已经结束，它留下的 owner 是陈旧的；交棒时把该角色释放掉，repair 重入才
    #     不会被「仍然活着的 owner」拒掉（否则停在第一次重入，止于
    #     `service_owner_claim_refused`）；
    #   * 新增 `_end_round_instance()`：结束一个实例的顺序是 **close → release → reap**，
    #     reap 放在最后（那时本轮已关闭并释放，新一轮不会经由旧注册表被波及），
    #     而 `terminal=False` 的**交棒不 reap**（修复轮还要接着用这些服务）；
    #   * 新增 `SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON` 与 `_terminal_write_ends_the_chain()`：
    #     「拒绝认领」是一条自己就终结链的理由；该判定规则从
    #     `_persist_verification_terminal` 里提出成独立函数，录制方与钉住它的测试
    #     读同一处，不会各自漂移；
    #   * VP-008 的**安全侧收敛**：`claim` / `end` 对**非字符串 token** 一律不作为
    #     （数字、布尔、列表、字典、bytes 都不指名任何一轮），对**不存在的 plan_dir**
    #     一律不落盘（注册表写入会把它要写的目录建出来）。
    # 改动相对上一版冻结基线（4cb12a87…）为 +536/-22，全部落在本文件，没有夹带别的改动；
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希 —— 哈希取 VP-008 落地
    # **之后**的读数（先钉 c4bc9b12… 会被 VP-008 的一次落地再次漂移）。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围
    # （仍是 `frontend/app.js` / `frontend/style.css` / `backend/verification_loop.py`
    # 这三个，一个不多一个不少）与 sha256 逐字节比对的强度都不变，该文件此后仍被
    # 逐字节钉住，任何再编辑照样红。
    # 2026-11-14：再次合并 origin/main（980249a5，PR #154「轮次实例服务所有权改用
    # 每次实例 UUID」）—— 这一次 ``backend/verification_loop.py`` 两侧仍然都改过：
    # 本分支继续删镜像写函数，main 侧带来上面那整套「轮次实例身份」改动。
    # 合并后的文件内容与两侧各自的冻结基线（``a07256be…`` / ``3207acac…``）都不同，
    # 因此按本 gate 的既定流程把冻结基线再前移到合并态哈希。
    # 前移之前已核对：合并后两侧的改动都还在，没有互相抵消 ——
    #   * main 侧：``_new_round_instance_token`` / ``_claim_round_instance_ownership`` /
    #     ``_release_handover_service_owner`` / ``_end_round_instance`` /
    #     ``SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON`` / ``_terminal_write_ends_the_chain``
    #     全部在位（``git diff d66d163f 980249a5 -- backend/verification_loop.py``
    #     的新增全部落进了合并结果 —— 注意上面那两个 ``a07256be…`` / ``3207acac…``
    #     是**文件内容**的 sha256、不是 git commit（``git cat-file -t`` 会说
    #     "Not a valid object name"），所以核对要拿那两个 commit 跑 diff，
    #     拿内容哈希去跑会直接报错）；
    #   * 本分支：镜像写函数 ``_update_plan_state_to_terminal`` 仍然整体消失 ——
    #     ``git diff 980249a5 -- backend/verification_loop.py`` 只剩镜像写代码的删除，
    #     没有别的，权威行仍由 ``_persist_verification_terminal`` 写；
    #   * 两处改动落在不同的代码段上（main 侧把 ``_chain_ending`` 的判定提成
    #     ``_terminal_write_ends_the_chain()``，本分支删的是 step 4 的镜像写段），
    #     合并结果两者都在。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑
    # 照样红。
    # 2026-11-14：本分支（base drift 轮次复用与审计、服务放置复用自家实例）的
    # 已批准改动同样落在 `verification_loop.py`；与上面「轮次实例身份」的改动
    # 合并后，冻结基线前移到**合并态**哈希。判定强度不变：仍是逐字节 sha256 比对。
    # 2026-11-14（本次合并 origin/main@316b360a5，PR #155「base drift 不消耗轮次，
    # 服务放置复用本 plan 自家实例」）：``backend/verification_loop.py`` 两侧又都改过
    # —— 本分支继续删镜像写函数 ``_update_plan_state_to_terminal``（权威行取代它），
    # base 侧带来 base drift 的轮次复用与审计。两侧各自的冻结基线（``1d338d27…`` /
    # ``1275d125…``）都与合并态内容不同，因此按本 gate 的既定流程把冻结基线前移到
    # 合并态哈希 ``9b884519…``。
    # 前移之前已核对：两侧的改动都还在，没有互相抵消 ——
    #   * base 侧：``_new_round_instance_token`` / ``_claim_round_instance_ownership`` /
    #     ``_release_handover_service_owner`` / ``_end_round_instance`` /
    #     ``SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON`` / ``_terminal_write_ends_the_chain``
    #     / ``mark_round_blocked`` 全部在位；
    #   * 本分支：镜像写函数 ``_update_plan_state_to_terminal`` 仍然整体消失（该文件里
    #     0 处引用），权威行仍由 ``_persist_verification_terminal`` 写。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围
    # 与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-14（本次合并 origin/main@ecc5d9ee）：``backend/verification_loop.py``
    # 两侧**都没有再改**这个文件 —— 合并基线 ``33041be4`` 的副本与 base 侧的副本
    # 逐字节相同（sha256 ``1275d125…``）；改它的仍然只有本分支那一侧（镜像写函数
    # ``_update_plan_state_to_terminal`` 整段删除），所以合并结果取的就是本分支这一
    # 侧，其真实 sha256 是 ``9b884519…``（即 ecc5d9ee 那次合并后所钉的值），
    # **本条哈希在 ecc5d9ee 那次合并后保持不变**。核对依据：``git diff ecc5d9ee --
    # backend/verification_loop.py`` 只剩镜像写代码的删除（+2/-169），没有别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围
    # 与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
#:
    # 2026-11-14：本分支（base drift 轮次复用与审计、服务放置复用自家实例）的
    # 已批准改动同样落在 `verification_loop.py`；与上面「轮次实例身份」的改动
    # 合并后，冻结基线前移到**合并态**哈希。判定强度不变：仍是逐字节 sha256 比对。
    # 2026-11-15：VP-016「未验证轮次的编号必须归还」这条判据的已批准改动落在
    # `verification_loop.py` 上 ——
    #   * `_release_unverified_round` 增加可选的 `report` 参数：轮次**测出过东西**
    #     （VP 结论为 PASSED / FAILED / PARTIAL）时，helper 自己拒绝归还并返回
    #     `False`。「只归还没派发任何 VP 的轮次」从此是 helper 守住的不变量，
    #     不再靠调用点的自觉；
    #   * 接上四个调用点，且都在该路径的**终局写入之后**归还：service preflight
    #     挡下、base drift 冲突停止、base drift 修复交接（在
    #     `_record_base_drift_round_block` 之后、`_run_repair_execution_async`
    #     之前）、循环收尾（理由由 `_round_release_reason` 推导）。
    #     binary freshness 预检那条路径不在判据的四条路径内，未接调用点。
    # 改动相对上一版冻结基线（dce06c54…）全部落在本文件，没有夹带别的改动；
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希。这是「移动冻结」
    # 而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围与 sha256
    # 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-15：VP-030「死掉的执行器不得给『验证循环还开着』的计划盖上工作流
    # 终局相位」这条判据的已批准改动落在 `verification_loop.py` 上 ——
    #   * 新增 `_read_verification_row_for_dead_phase`：读该计划的
    #     `plan_verification` 行，并把「没有验证行」（absent）与「读不到验证行」
    #     （unreadable）分成两个不同的结论 —— 后者是**未知**，未知不能被当成结论；
    #   * 新增 `_dead_phase_may_be_stamped`：崩溃裁决只有在验证循环**已有结论**
    #     （`passed` / `failed`）或**根本没有跑过**（无验证行）时才允许盖
    #     `'failed'`；`pending` / `running` / `loop_stopped`（循环还开着）保留原相位，
    #     读不到行或状态不在词表内一律按未知处理 —— 不盖终局相位并记一条 warning；
    #   * `_mark_failed_dead` 的相位写入改为「原判据（相位不在终局词表内）与新闸门
    #     取与」，且新闸门调用**移出**原来那条 `except Exception: pass` —— 读验证行
    #     失败这条路径上的告警必须是可见的，不能被恢复路径吞掉。
    # 执行行上的死亡裁决（`exec_status='failed'` / `stop_reason=
    # 'process_died_unexpectedly'` / `exec_pid=None`）、`in_progress` 回收与幂等闸
    # 一字未动。
    # 改动相对上一版冻结基线（18931972…）全部落在本文件，没有夹带别的改动；
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希。这是「移动冻结」
    # 而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围与 sha256
    # 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-15：因本分支已批准的「停放不等于失败」（PRD R1）改动落在
    # `verification_loop.py` 上，按本 gate 的既定流程把冻结基线前移到本分支的
    # 批准态哈希：
    # 2026-11-16：task-16 的已批准改动落在本文件上 —— f0cd9c5f「把收尾门控接入验证
    # 通过出口」与 d592a6da「补齐收尾阶段的终态出口：终态相位与失败通知通道」；
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希（`shasum -a 256`
    # 实测值）。表内条目集合与断言强度不变：被钉文件的范围不变、判定强度仍是逐字节
    # sha256 比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-16（合入 origin/main 78ffdddc / PR #168）：main 侧对同一文件也有已批准
    # 改动 ——「停放不等于失败」（PRD R1）：
    #   * task-4（a9bd275e「死亡闩锁不把配额停放判成线程死亡」）：新增
    #     `_plan_parked_for_quota`，在 `verification_thread_died_unexpectedly`
    #     判死**之前**先问一句「这个计划是不是停在配额上」；命中即不是死亡 ——
    #     不写终态、不动相位，计划停在 waiting_for_quota，等配额窗口重开后续跑；
    #   * task-5（566ec593「验证轮停放写入行与运行时旁路文件」）：新增
    #     `_park_verification_round_for_quota`，让验证侧的停放有地方落行。
    # 前移之前已核对：自上一版基线（dbf9a037，2026-11-14 钉住的合并态）以来，
    # 该文件只有上述两处已批准改动（+294/-0，分别来自 task-4 与 task-5），
    # 没有夹带别的改动。
    # 两侧改动落在同一文件的**不同位置**，自动合并无冲突，两侧的意图都保留；按本
    # gate 的既定流程把冻结基线前移到**合并态**哈希（`shasum -a 256` 实测值）。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的
    # 范围（仍是 `frontend/app.js` / `frontend/style.css` / `backend/verification_loop.py`
    # 这三个，一个不多一个不少）与 sha256 逐字节比对的强度都不变，该文件此后仍被
    # 逐字节钉住，任何再编辑照样红。
    # 2026-11-16（本次合并 origin/main@78ffdddc，7 个新提交）：``backend/verification_loop.py``
    # 两侧**又都改过** —— 本分支继续删镜像写函数 ``_update_plan_state_to_terminal``
    # （权威行取代它），base 侧带来 2026-11-15 的「停放不等于失败」（PRD R1：
    # ``_plan_parked_for_quota`` / ``_park_verification_round_for_quota``）。
    # 合并后的文件内容与两侧各自的冻结基线（``9b884519…`` / ``bda62e8b…``）都不同，
    # 因此按本 gate 的既定流程把冻结基线再前移到合并态哈希 ``438a0c7c…``。
    # 前移之前已核对：两侧的改动都还在，没有互相抵消 ——
    #   * base 侧：``_plan_parked_for_quota`` / ``_park_verification_round_for_quota``
    #     都在位（该文件里各 2 处引用），``_new_round_instance_token`` /
    #     ``_claim_round_instance_ownership`` / ``_release_handover_service_owner`` /
    #     ``_end_round_instance`` / ``mark_round_blocked`` /
    #     ``_terminal_write_ends_the_chain`` / ``SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON``
    #     也都在位；
    #   * 本分支：镜像写函数 ``_update_plan_state_to_terminal`` 仍然整体消失（该文件里
    #     0 处引用），权威行仍由 ``_persist_verification_terminal`` 写。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围
    # 与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-17（本次合并 origin/main@b9b9061b，4 个新提交）：``backend/verification_loop.py``
    # 两侧**又都改过** —— 本分支继续删镜像写函数 ``_update_plan_state_to_terminal``
    # （权威行取代它），base 侧带来收尾阶段（postprocess）的改动
    # （``_start_postprocess`` / ``_postprocess_notification_sink`` /
    # ``_settle_postprocess_phase``，+208/-4）。
    # 合并后的文件内容与两侧各自的冻结基线（``438a0c7c…`` / ``c3cc3314…``）都不同，
    # 因此按本 gate 的既定流程把冻结基线再前移到合并态哈希 ``541f6984…``。
    # 前移之前已核对：两侧的改动都还在，没有互相抵消 ——
    #   * base 侧：``_plan_parked_for_quota`` / ``_park_verification_round_for_quota``
    #     都在位（该文件里各 2 处引用），``_new_round_instance_token`` /
    #     ``_claim_round_instance_ownership`` / ``_release_handover_service_owner`` /
    #     ``_end_round_instance`` / ``mark_round_blocked`` /
    #     ``_terminal_write_ends_the_chain`` / ``SERVICE_OWNER_CLAIM_REFUSED_STOP_REASON``
    #     也都在位，收尾阶段的三个新函数同样在位；
    #   * 本分支：镜像写函数 ``_update_plan_state_to_terminal`` 仍然整体消失（该文件里
    #     0 处引用），权威行仍由 ``_persist_verification_terminal`` 写。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围
    # 与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
#:
    # 2026-11-17（本次合入 origin/main@78ffdddc）：base 侧的「停放不等于失败」
    # （`_plan_parked_for_quota` / `_park_verification_round_for_quota`）与本分支的
    # 「轮次归还」（`_release_unverified_round` 及其调用点）与「崩溃不盖终局相位」
    # （`_dead_phase_may_be_stamped`）落在**不同位置**，互不覆盖，故两边都保留；
    # 文件内容因此再次变化，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-17（本次合入 origin/main@b9b9061b —— PR #170「验证通过后的收尾阶段」）：
    # base 侧在 78ffdddc 之后又给本文件接上了「验证通过 → 收尾阶段」的出口
    # （`_start_postprocess` / `_postprocess_notification_sink` /
    # `_settle_postprocess_phase`），与本分支的「轮次归还」（`_release_unverified_round`
    # 及其调用点）和「崩溃不盖终局相位」（`_dead_phase_may_be_stamped`）落在**不同
    # 位置**，互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的既定流程把
    # 冻结基线前移到**合并态**哈希（`shasum -a 256` 实测值）。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-19：因「修复完成后的通过出口走回收尾判定点」这条已批准改动落在
    # `verification_loop.py` 上 —— 修复执行完成后的 `passed` 出口不再自己写终态，
    # 改为调用唯一的收尾判定点 `_handle_passed_round`（有要求书的计划因此进入
    # `post_processing`，不再被直接写成 `completed`）。改动只落在
    # `_make_repair_on_complete` 的 `passed` 分支一处，其它分支
    # （failed / loop_stopped / split）与 `_handle_passed_round` 内部逻辑一字未动。
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希（`shasum -a 256`
    # 实测值）。这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、
    # 被钉文件的范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-21（本次合并 origin/main@daf1168c）：两侧都改过该文件 ——
    # base 侧带来「修复执行器派发前加闸门」与停止语义重构（`_run_repair_execution_async`
    # 在 spawn 前读 plan 级停止记录；停止流程改为先关闸门、逐层协作停止、静默后翻相位）；
    # 本分支带来「删镜像写函数」等改动。两侧改动落在不同位置，合并后实测两侧都在位：
    #   * base 侧：`_run_repair_execution_async` 的停止闸门、`_handle_passed_round`、
    #     `_new_round_instance_token` / `_claim_round_instance_ownership` /
    #     `_terminal_write_ends_the_chain` / `_start_postprocess` /
    #     `_plan_parked_for_quota` / `_park_verification_round_for_quota` 均在位；
    #   * 本分支：镜像写函数 `_update_plan_state_to_terminal` 仍为 0 处引用，
    #     权威行仍由 `_persist_verification_terminal` 写。
    # 文件内容因此变化，按本 gate 的既定流程把冻结基线前移到合并态哈希
    # （`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 仍逐字节比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-22：因已批准的「有失败项、却 0 条修复任务 → 交人工裁定」改动了
    # `verification_loop.py` —— 新增 stop_reason
    # `FAILURES_WITHOUT_REPAIR_TASKS_STOP_REASON` 与对应的分支（本轮确实有失败项、
    # 修复队列却一条任务都没有时，不再回到 `executing` 空转，改为以
    # `chain_ending=True` 结束链条并交给人工裁定）。改动只落在该分支一处，
    # 空修复队列的其余行为一字未动。
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希（`shasum -a 256`
    # 实测值）。这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、
    # 被钉文件的范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    # 2026-11-22（本次：收尾阶段「有上限的续跑入口」）：已批准的改动落在
    # `verification_loop.py` 上 —— 新增模块级 `resume_postprocess(plan_id)`，
    # 它照 `_start_postprocess` 的形状写，**唯一**区别是 `orchestrator.resume()`
    # 而不是 `orchestrator.start()`（`resume()` 只重跑进度文件里未完成/失败的条目，
    # `start()` 才会重算第一轮）；线程、懒导入、`finally` 里
    # `_settle_postprocess_phase` 的写法与 `_start_postprocess` 一致。
    # `_start_postprocess` 本身与其它函数的字节**一字未动**。
    # 按本 gate 的既定流程把冻结基线前移到本分支的批准态哈希（`shasum -a 256`
    # 实测值）。这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、
    # 被钉文件的范围与 sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，
    # 任何再编辑照样红。
    "backend/verification_loop.py": (
        "2b085ab0e430a3f1b91172dde96116d24af90aff2d3a8870593d425e85d4d95c"
    ),
}

#: The native scheduler's own surface, frozen the same way.  Two files
#: under the forbidden paths change on this branch by design:
#:
#:   * ``backend/state_machine/db/schema.py`` — schema v7 adds the
#:     four ``scheduler_*`` tables (and bumps ``CURRENT_SCHEMA_VERSION``
#:     from 6 to 7).  The state machine is shared with the rest of the
#:     product, so the change is excluded from the change-surface diff
#:     and pinned here instead, byte for byte.
#:   * ``frontend/index.html`` — the scheduler view's markup.
#:
#: Both are excluded from the diff pathspecs above and both are held
#: by the hashes below, so the exclusion does not weaken the gate: a
#: later edit to either file fails ``test_scheduler_surface_is_frozen``
#: even though it no longer shows up in the diff.
FROZEN_SCHEDULER_SURFACE_SHA256: dict[str, str] = {
    # 2026-10-29：因新增 `scheduler_setting` 表（容量的运行期覆盖）改动本文件，
    # 按本 gate 的既定流程重新钉住该文件的哈希；schema.py 的模块 docstring
    # 已同步改为「four ``scheduler_*`` tables」。
    # 2026-11-02：因「victim 优先级」给 `scheduler_slot` 加了 `priority` 列
    # （`priority INTEGER NOT NULL DEFAULT 0`，并对老库做 presence-guarded 的
    # `ALTER TABLE`，schema 版本仍为 7）改动本文件，按本 gate 的既定流程重新
    # 钉住该文件的哈希。schema.py 的内容变化是这一条 DDL 加上它的说明与升级
    # 步骤；DDL 层面的 delta 只有一个列，没有别的表或列动过。
    #
    # 2026-11-04（根因报告交付）：`plan_tasks` 新增 `deliverable` / `report_path`
    # 两列（报告型任务的交付通道：交付物是一份文件，地址在 `report_path`），
    # 同一次改动，按本 gate 的既定流程重新钉住该文件的哈希。
    #
    # 合并 origin/main 后两侧的改动同时在 schema.py 里：既有 `scheduler_slot`
    # 的 `priority` 列，也有 `plan_tasks` 的那两列。文件哈希因此与两侧都不同，
    # 按本 gate 的既定流程重新钉住合并态的哈希。这是「移动冻结」而不是「放松
    # 门禁」——断言与逐字节比对的强度都不变，该文件此后仍被逐字节钉住。
    "backend/state_machine/db/schema.py": (
        "69de90baf6f5f2dbae8584ef82cbf3abdd787d4675ee8c36c7abe6746c26a408"
    ),
    # 2026-10-30：因调度面板改版（视图重排、在跑运行时长、并行 Agent 数、容量下拉框）
    # 改动本文件，按本 gate 的既定流程重新钉住该文件的哈希。
    # 2026-10-31（合入 main）：调度面板改版之上多回了「终态计划」区块 ——
    # failed 计划既不在队列里也不在「在跑」里，failed 相位的两个按钮画在
    # 这个区块里；同样按本 gate 的既定流程重新钉住哈希。
    # 2026-11-01：因「调度卡片容器与收起态样式」（task-11）改动了 `frontend/index.html`，
    # 按本 gate 的既定流程把哈希前移到本分支的批准态；表内条目集合不变。
    # 2026-11-09：448d67df（2026-11-08）「应用后以服务端应答的数字为准」在调度面板的
    # 标记里增加一处 `#scheduler-capacity-notice` 提示位（+1 行），用来显示
    # 「服务端规整为 N」——服务端给的数字与提交的不同时，提示位把那个数字说出来，
    # 而不是静默跳回。该文件的改动全部是这一条已批准的需求（R4「容量下拉框不得自己
    # 跳数」），没有夹带别的改动，按本 gate 的既定流程把冻结基线前移到本分支的
    # 批准态。
    # 前移之前已核对：自上一版基线（ad50f9d0，即 2026-10-31 钉住的状态）以来，
    # 该文件只有 448d67df 这一处改动（+1 行），没有夹带别的改动。
    # 这是「移动冻结」而不是「放松门禁」：断言（mismatched == []）、被钉文件的范围、
    # sha256 逐字节比对的强度都不变，该文件此后仍被逐字节钉住，任何再编辑照样红。
    # 2026-11-14：合入 origin/main（316b360a）—— 两侧的改动落在同一文件的
    # **不同位置**：main 的「容量下拉框不得自己跳数」在标记里增加一处
    # `#scheduler-capacity-notice` 提示位，与本分支的「调度卡片容器与收起态样式」
    # 互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的既定流程把
    # 冻结基线前移到**合并态**哈希。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 仍逐字节比对。
    # 2026-11-14（本次合并 origin/main@ecc5d9ee）：本分支自合并基线 `33041be4`
    # 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同（sha256
    # `d98d19a9…`），改它的只有 base 侧。因此合并后本文件的内容与 base 侧逐字节
    # 相同，其真实 sha256 就是下方所钉的 `b5472f09…`，按本 gate 的既定流程把冻结
    # 基线前移到**合并态**哈希。前移的两端都记在这里：基线由 `d98d19a9…`
    # （合并前的批准态）移到 `b5472f09…`（合并后的批准态）。表内条目集合与断言
    # 强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-16：task-23「调度页新增等待区块与要求书编辑入口」（4dc2f275）在标记里
    # 增加了调度面板的等待区块（`#scheduler-waiting-*`）与要求书编辑区
    # （`#scheduler-spec-editor`），task-repair-01 的合并解冲突（b8a421f1）又落在同一
    # 文件，两侧的意图都保留；按本 gate 的既定流程把冻结基线前移到**合并态**哈希
    # （`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 仍逐字节比对。
    # 2026-11-17（本次合并 origin/main@b9b9061b，4 个新提交）：本分支自合并基线
    # `78ffdddc` 分叉以来**没有改过**这个文件 —— 基线与本分支的副本逐字节相同
    # （sha256 `b5472f09…`），改它的只有 base 侧（等待区块与要求书编辑区的标记：
    # `#scheduler-waiting-*` / `#scheduler-spec-*`，+42/-2）。
    # 因此合并后本文件的内容与 base 侧逐字节相同，其真实 sha256 就是下方所钉的
    # `f8c07759…`，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 前移的两端都记在这里：基线由 `b5472f09…`（合并前的批准态）移到
    # `f8c07759…`（合并后的批准态）。表内条目集合与断言强度不变：被钉文件的
    # 范围不变、sha256 逐字节比对的强度不变。
#:
    # 2026-11-17（本次合入 origin/main@b9b9061b —— PR #170「验证通过后的收尾阶段」）：
    # 该文件只被 base 侧改动（本分支未动它），合并后的内容与 base 侧一致；按本 gate
    # 的既定流程把冻结基线钉在**合并态**哈希（`shasum -a 256` 实测值）。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 逐字节比对。
    # 2026-11-23：因本次改动在调度视图里新增「收尾」分区（`scheduler-finishing-cards`
    # / `scheduler-finishing-empty`）并删掉「要求书」编辑框，改动了
    # `frontend/index.html`，按本 gate 的既定流程把冻结基线前移到本分支的批准态。
    # 哈希取自工作区文件的 `shasum -a 256` 实测值，不是抄来的。表内条目集合与断言
    # 强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    "frontend/index.html": (
        "f219545e11e47aacae6b011adcc025188b6a2ce8d3da1b8fcf6f24019325486d"
    ),
}

#: The capacity-wait status vocabulary.  The execution repository is
#: where an execution's status is written, and the capacity wait needs
#: a status of its own (``waiting_for_quota``) — a pause that is
#: neither a failure nor a cancellation.  The file is therefore
#: excluded from the change-surface diff (above) and frozen here
#: instead, byte for byte: the diff gate stops reporting the one
#: approved change, and this gate stops reporting any *other* change
#: to the same file.
FROZEN_CAPACITY_WAIT_SURFACE_SHA256: dict[str, str] = {
    # 2026-11-08：`update_status` 增加可选的 `stop_reason`（executor 死亡取证）——
    # 只在调用方给了理由时才写这一列，没给就原样保留；这是已批准的改动，
    # 按本 gate 的既定流程重新钉住该文件的哈希。这是「移动冻结」而不是
    # 「放松门禁」：被钉文件的范围、逐字节比对的强度都不变，该文件此后
    # 仍被逐字节钉住，任何再编辑照样红。
    "backend/state_machine/repositories/execution_repository.py": (
        "b001a889523b17840c35a4dd859e1d5bcc07894c58c39ca25d53d4831c149e68"
    ),
}

#: The round-release surface, frozen the same way.  The round counter
#: lives in the verification repository: ``init_round`` overwrites it and
#: there was no way back, so a round that was blocked before any
#: verification point was dispatched still spent one of the plan's
#: rounds.  ``release_round`` is that way back, and ``_decode_executor_state``
#: is the tolerant reader it needs to merge into the free-form
#: ``executor_state`` blob.  The file is therefore excluded from the
#: change-surface diff (above) and frozen here instead, byte for byte:
#: the diff gate stops reporting the one approved change, and this gate
#: stops reporting any *other* change to the same file.
FROZEN_VERIFICATION_REPOSITORY_SURFACE_SHA256: dict[str, str] = {
    # 合并 origin/main 后该文件两侧都改过（分支新增 `release_round` 与
    # `_decode_executor_state`，main 侧带来 plan_id 列等改动），文件哈希与两侧
    # 都不同，按本 gate 的既定流程重新钉住合并态的哈希。这是「移动冻结」而不是
    # 「放松门禁」——断言与逐字节比对的强度都不变。
    # 2026-11-02：因「没测出东西的轮次不占编号」在仓储层新增 `release_round`
    # （归还轮次编号并把原因记进 `executor_state["released_rounds"]`）与
    # `_decode_executor_state`（把 `executor_state` 读成 JSON 对象的容错读取器），
    # 按本 gate 的既定流程钉住该文件的哈希。这对应 verification convergence
    # PRD 的 §3 一条：一个没有派发任何 VP 的轮次不得消耗掉一个轮次编号。
    # 2026-11-14（本次合入 origin/main）：两侧的改动落在同一文件的**不同位置** ——
    # 本分支的 `release_round` / `_decode_executor_state` 与 base 侧的
    # `_stop_reason_is_write_once` / `mark_round_blocked` 互不覆盖，故两边都保留；
    # 文件内容因此再次变化，按本 gate 的既定流程把冻结基线前移到**合并态**哈希。
    # 表内条目集合与断言强度不变：被钉文件的范围不变、sha256 仍逐字节比对。
    # 2026-11-17（本次合入 origin/main@78ffdddc）：base 侧的「配额停放写行」
    # （PR #168 的 `_park_verification_round_for_quota` 与 plan_id 列等改动）与本分支
    # 的 `release_round` / `_decode_executor_state` 落在同一文件的**不同位置** ——
    # 互不覆盖，故两边都保留；文件内容因此再次变化，按本 gate 的既定流程把冻结
    # 基线前移到**合并态**哈希。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 仍逐字节比对。
    # 2026-11-19（本次合并 origin/main@021565b9）：该文件两侧都改过 —— 本分支的
    # `release_round` / `_decode_executor_state` 与 base 侧的 `mark_round_blocked` /
    # `_stop_reason_is_write_once` 落在**不同位置**，互不覆盖，故两边都保留；文件
    # 内容因此再次变化，按本 gate 的既定流程把冻结基线前移到**合并态**哈希
    # （`shasum -a 256` 实测值）。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 仍逐字节比对。
    "backend/state_machine/repositories/verification_repository.py": (
        "1c005254958ddb3167d6d31936718a82e4183eee95c24f5b4e51c51fbb07057a"
    ),
}

#: The routing-mirror surface, frozen the same way.  The routing row's
#: ``verification`` column used to carry the verification state; this
#: plan moved the authoritative write of that state to the
#: ``plan_verification`` table, and the column now carries only the
#: frozen baseline.  The file is therefore excluded from the
#: change-surface diff (above) and frozen here instead, byte for byte:
#: the diff gate stops reporting the one approved change, and this gate
#: stops reporting any *other* change to the same file.
FROZEN_ROUTING_REPOSITORY_SURFACE_SHA256: dict[str, str] = {
    # 2026-11-19（本次合并 origin/main@021565b9 之后）：该文件只有本分支一侧的
    # 已批准改动 —— ``write_plan_state`` 不再写 ``verification`` 镜像列，
    # ``write_verification_baseline`` 成为该列唯一的写者（镜像列只剩冻结基线）。
    # base 侧未改这个文件，故合并后的内容与本分支逐字节相同，其真实 sha256 就是
    # 下方所钉的值（`shasum -a 256` 实测值），按本 gate 的既定流程把冻结基线
    # 钉在**合并态**哈希。表内条目集合与断言强度不变：被钉文件的范围不变、
    # sha256 仍逐字节比对，该文件此后仍被逐字节钉住，任何再编辑照样红。
    "backend/state_machine/repositories/routing_repository.py": (
        "bda605564ba0d5bb6e8d78b656e3e2461da77f44f13c9931614865010a9d6396"
    ),
}


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

    A few files are excluded from this diff, each for one approved
    change that reaches shared code: ``frontend/app.js``,
    ``frontend/style.css`` and ``backend/verification_loop.py`` (the
    phase merge renamed the waiting bucket to ``queued``),
    ``frontend/index.html`` and
    ``backend/state_machine/db/schema.py`` (the native scheduler),
    ``backend/state_machine/repositories/execution_repository.py`` (the
    capacity-wait status vocabulary) and
    ``backend/state_machine/repositories/verification_repository.py``
    (the round release).  The exclusions are deliberate and narrow:
    every one of those files is frozen byte for byte by its own
    ``test_*_surface_is_frozen`` gate, so this gate's strength does not
    drop — it moves from "no change at all" to "no change beyond the one
    that was approved". Everything else under the forbidden paths is
    still frozen here.
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


def test_every_scoped_exclusion_is_documented() -> None:
    """Every narrow exclusion is justified in prose.

    A ``:(exclude)`` pathspec takes a file out of the change-surface
    diff. The diff then stops being able to say anything about that
    file, so the file's exclusion has to be accounted for in words:
    which approved change it carries, and what holds it still.

    This gate keeps that accounting complete. The block of prose that
    documents ``FORBIDDEN_MODULE_PATHS`` is the one place a reader
    looks for it, so every scoped exclusion has to be named there —
    otherwise a new exclusion arrives as a bare pathspec line and the
    only trace of the reason is a comment buried inside the tuple.

    The rule does not widen or narrow the change surface; it keeps the
    file's own description of that surface true.
    """
    documentation = exclusion_documentation()
    undocumented = [
        path
        for path in scoped_exclusion_paths()
        if path not in documentation
    ]
    assert undocumented == [], (
        "a scoped exclusion is not accounted for in the block that "
        "documents FORBIDDEN_MODULE_PATHS:\n  " + "\n  ".join(undocumented)
    )


def test_r10_waiting_phase_rename_surface_is_frozen() -> None:
    """The phase merge's rename surface is frozen byte for byte.

    The rename of the waiting bucket to ``queued`` reaches three shared
    files, which is why they are excluded from the change-surface diff.
    Excluding a file from a diff is only safe if something else holds
    it still: this gate pins every byte of the three files to the hash
    of the approved state, so any further edit to them fails here even
    though it no longer shows up in the diff.

    The gate does not weaken the change-surface rule; it moves it from
    "these files never change" to "these files change once, and only
    that way".
    """
    mismatched = [
        f"{rel}: frozen {expected}, actual "
        f"{hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()}"
        for rel, expected in FROZEN_R10_SURFACE_SHA256.items()
        if hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() != expected
    ]
    assert mismatched == [], (
        "a file in R10's waiting-phase rename surface changed outside "
        "the frozen baseline:\n  " + "\n  ".join(mismatched)
    )


def test_frozen_hash_matches_verification_loop() -> None:
    """The pin on ``backend/verification_loop.py`` is a measurement.

    The gate above compares the table against the disk, but it compares
    the table as it was IMPORTED — and an imported constant is whatever
    the source says it is, including a value that was typed by hand or
    left behind by an earlier refresh.  This one reads the literal out
    of THIS file's own source text and compares it with the bytes on
    disk, so the value that is checked is the value that is actually
    written down.

    It is the same rule the sibling contract gate applies to the three
    frontend files it refreshed (``test_frozen_hash_refresh_contract``),
    applied to the one pin this round moved: the repair dispatch's spawn
    gate lives in ``backend/verification_loop.py``, so that file's
    content changed and its pin had to move with it.
    """
    source = Path(__file__).resolve().read_text(encoding="utf-8")
    match = re.search(
        r'"backend/verification_loop\.py":\s*\(\s*"([0-9a-f]{64})"\s*\)',
        source,
    )
    assert match is not None, (
        "the frozen hash of backend/verification_loop.py is no longer "
        "written down in this file — the pin was dropped, not moved"
    )
    frozen = match.group(1)
    actual = hashlib.sha256(
        (REPO_ROOT / "backend/verification_loop.py").read_bytes()
    ).hexdigest()
    assert frozen == actual, (
        "the frozen hash of backend/verification_loop.py does not match "
        f"the file on disk:\n  frozen: {frozen}\n  actual: {actual}"
    )


def test_capacity_wait_status_surface_is_frozen() -> None:
    """The capacity wait's status vocabulary is frozen byte for byte.

    The execution repository is where an execution's status is written,
    and the capacity wait needed a status of its own: a run parked
    because the provider has no quota left is neither a failure nor a
    cancellation, and the five-value set had no word for it.  That is
    the approved change, and the file is therefore excluded from the
    change-surface diff — which is only safe if something else holds
    it still.  This gate pins every byte of the file to the hash of the
    approved state, so any *further* edit to it fails here even though
    it no longer shows up in the diff.

    The rule does not weaken the change-surface gate; it moves it from
    "this file never changes" to "this file changes once, and only
    that way".
    """
    mismatched = [
        f"{rel}: frozen {expected}, actual "
        f"{hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()}"
        for rel, expected in FROZEN_CAPACITY_WAIT_SURFACE_SHA256.items()
        if hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() != expected
    ]
    assert mismatched == [], (
        "a file in the capacity wait's status surface changed outside "
        "the frozen baseline:\n  " + "\n  ".join(mismatched)
    )


def test_scheduler_surface_is_frozen() -> None:
    """The native scheduler's own surface is frozen byte for byte.

    Two files under the forbidden paths change on this branch by
    design: the schema module that adds the three ``scheduler_*``
    tables, and the frontend markup that carries the scheduler view.
    Both are excluded from the change-surface diff, so something else
    has to hold them still — this gate does, by pinning every byte of
    each to the hash of the approved state.  Any *further* edit to
    either file fails here even though it no longer shows up in the
    diff.

    The rule does not weaken the change-surface gate; it moves it from
    "these files never change" to "these files change once, and only
    that way".
    """
    mismatched = [
        f"{rel}: frozen {expected}, actual "
        f"{hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()}"
        for rel, expected in FROZEN_SCHEDULER_SURFACE_SHA256.items()
        if hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() != expected
    ]
    assert mismatched == [], (
        "a file in the native scheduler's surface changed outside the "
        "frozen baseline:\n  " + "\n  ".join(mismatched)
    )


def test_verification_repository_surface_is_frozen() -> None:
    """The round-release surface is frozen byte for byte.

    The round counter lives in the verification repository, and a round
    that measured nothing has to give its number back: ``init_round``
    overwrites the counter and there was no way back, so the plan spent
    one of its rounds on a turn that never produced a verdict.  That
    is the approved change — ``release_round`` plus the tolerant
    ``_decode_executor_state`` reader — and the file is therefore
    excluded from the change-surface diff, which is only safe if
    something else holds it still.  This gate pins every byte of the
    file to the hash of the approved state, so any *further* edit to it
    fails here even though it no longer shows up in the diff.

    The rule does not weaken the change-surface gate; it moves it from
    "this file never changes" to "this file changes once, and only
    that way".
    """
    mismatched = [
        f"{rel}: frozen {expected}, actual "
        f"{hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()}"
        for rel, expected in FROZEN_VERIFICATION_REPOSITORY_SURFACE_SHA256.items()
        if hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() != expected
    ]
    assert mismatched == [], (
        "a file in the round-release surface changed outside the frozen "
        "baseline:\n  " + "\n  ".join(mismatched)
    )


def test_routing_repository_surface_is_frozen() -> None:
    """The routing-mirror surface is frozen byte for byte.

    The routing row's ``verification`` column used to carry the
    verification state; this plan moved the authoritative write of that
    state to the ``plan_verification`` table, and the column now
    carries only the frozen baseline.  That change reaches shared code,
    so the file is excluded from the change-surface diff — which is
    only safe if something else holds it still.  This gate pins every
    byte of the file to the hash of the approved state, so any
    *further* edit to it fails here even though it no longer shows up
    in the diff.

    The rule does not weaken the change-surface gate; it moves it from
    "this file never changes" to "this file changes once, and only
    that way".
    """
    mismatched = [
        f"{rel}: frozen {expected}, actual "
        f"{hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()}"
        for rel, expected in FROZEN_ROUTING_REPOSITORY_SURFACE_SHA256.items()
        if hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() != expected
    ]
    assert mismatched == [], (
        "a file in the routing-mirror surface changed outside the frozen "
        "baseline:\n  " + "\n  ".join(mismatched)
    )
