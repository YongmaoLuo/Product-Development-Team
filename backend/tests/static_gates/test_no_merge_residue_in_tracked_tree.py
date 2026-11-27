"""No merge-resolution residue may sit in the tracked tree.

Why this gate exists
--------------------
Resolving a conflict leaves debris, and the debris is easy to commit by
accident because it looks like part of the project:

* a **diff fragment** parked next to the file it describes -- a file
  whose *name* carries the comparison it was made for and whose
  contents are patch text;
* a **prose-named file** -- a tracked path that is a *description* of a
  file rather than a name for one: it carries whitespace or non-ASCII
  characters, because what landed in the tree was a sentence about a
  file, not a file;
* a **``.orig``** or **``.rej``** file -- git's own copies of the
  pre-merge version and of a patch that did not apply;
* **conflict markers** left inside a source file -- the marker lines
  git writes into an unmerged file, which survive a careless
  ``git add``.

The first shape is the one that reached the tree. A file in the
verification package whose whole content is a patch against another
revision is importable, plausible and wrong: nothing imports it, so
every test still passes, and the tree quietly carries a second,
half-applied version of a change the real file already holds. A reader
cannot tell it from a deliberate sample, and a future contributor may
edit it believing it is live.

Why the index and not ``HEAD``
------------------------------
``git ls-files`` reports what the **index** holds. ``HEAD`` reports what
is already committed, and the framework takes its checkpoint commit
*after* this suite runs -- so a gate reading ``HEAD`` would read a tree
that does not yet contain the fix, and would stay red for the rest of
the branch. The index is the state the next commit is built from,
which is exactly the state that must not carry debris. (Same
reasoning, same directory, as ``test_e2e_artifacts_are_not_tracked.py``.)

The contents are read from the index as well: ``git ls-files -s`` gives
the blob id of every tracked path, and ``git cat-file --batch`` gives
the bytes of that blob. That judges what is committed -- but it is not
what the next commit carries, because the next commit is a blanket
``git add`` and takes the working tree's bytes. So the working tree is
read as well, and both texts are held to the same marker rule. A
merge left unresolved on disk is invisible to the index (which still
holds the pre-merge blob) and fatal to the commit that follows.

Every text file the index holds is read, and the gate names the ones
that could not be. A reader that silently drops files judges a
fraction of the tree and passes anyway -- and it passes in the
direction that lets debris through. A path with **no** extension
counts as text: ``.gitignore``, ``LICENSE``, ``.coveragerc`` and
``.bandit`` are all extensionless, and a conflict marker left in one
of them is just as much debris as one in a module.

Why the working tree as well as the index
-----------------------------------------
The index says what is tracked *now*. The framework's checkpoint commit
is a blanket ``git add``, and it runs *after* this suite -- so a debris
file that is merely waiting in the working tree passes a gate that
reads only the index, and lands in the commit anyway. That is not
hypothetical: the diff fragment this gate was written for was removed
from the index, was written back into the working tree, and the next
blanket add put it straight back into the tracked tree.

So the gate reads the untracked files too -- ``git ls-files --others
--exclude-standard`` -- and holds them to the same name rule. A file
that is not tracked yet but is already named like debris is debris in
waiting, and the gate says so while there is still time to act.

And because the blanket add takes the working tree's bytes, the
*content* of the tracked files is read from the working tree too, not
only from the index. The two are different texts: the index still
holds the pre-merge blob of a file whose merge was left unresolved on
disk. A gate that reads only the index therefore passes on exactly the
state that the next commit turns into debris.

What counts as a marker
-----------------------
A marker counts only when it **starts a line**. Git writes markers in
column zero; prose that names them is not a conflict. The repository has
a module that defines the marker string it searches for when it looks
for unmerged files, and a test that asserts on one -- flagging either
would make the gate cry wolf on the very code that exists to detect
conflicts.

The decision logic is a pure function over (paths, contents), so the
rules can be exercised with synthetic inputs; the gate that reads the
real tree is one call away from them.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

#: Repository root. This file lives at ``backend/tests/static_gates/``,
#: so the root is three parents up -- the same derivation the sibling
#: gates in this directory use.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: Path fragments that mark a file as conflict debris. Compared
#: case-insensitively against the whole path: the fragment the debris
#: carries is prose, not a filename convention.
RESIDUE_PATH_MARKERS: tuple[str, ...] = ("diff against",)

#: Path suffixes git gives the two files a failed conflict resolution
#: leaves behind -- the pre-merge copy, and the rejected patch.
RESIDUE_PATH_SUFFIXES: tuple[str, ...] = (".orig", ".rej")

#: The rule recorded for a path whose *name* carries whitespace.
#:
#: A tracked path with a space in it is not a file name anybody chose:
#: it is the shape a *description* of a file takes when it is written
#: into the tree by mistake -- ``test_foo.py （修改，1070 行）``. The
#: prose is what a merge writes down about a file, and it becomes a
#: path the moment it is staged. No extension rule reaches such a path:
#: its suffix is the whole tail of the prose.
RULE_PATH_WHITESPACE = "path-with-whitespace"

#: The rule recorded for a path whose *name* carries a non-ASCII
#: character. The same shape seen from the other side -- the prose is
#: written in the author's own script.
RULE_PATH_NON_ASCII = "non-ascii-path"

#: The three marker lines that make up a conflict block. Written as
#: fragments so that this file's own source does not carry a marker at
#: the start of a line -- the very shape the gate forbids.
CONFLICT_START = "<" * 7
CONFLICT_SEPARATOR = "=" * 7
CONFLICT_END = ">" * 7

#: Extensions whose index content is read and scanned for markers.
#: A path with no extension counts as text too -- see
#: :func:`is_text_entry`. Documentation formats are left out on
#: purpose: prose that quotes a conflict block is a document, not
#: debris. A patch file is the opposite: ``.diff`` and ``.patch`` are
#: the ordinary names of the diff fragment this gate exists for, and a
#: file the gate never reads is a file it cannot judge.
TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".html",
    ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash",
    ".sql", ".cfg", ".ini", ".example",
    ".diff", ".patch",
})

#: The kind recorded for a path whose *name* looks like debris.
KIND_RESIDUE_PATH = "residue-path"

#: The index mode of a gitlink -- a submodule. Its entry names a
#: commit, not a file: there is no content to read and no content to
#: judge.
GITLINK_MODE = "160000"

#: The kind recorded for a file that carries a conflict marker.
KIND_CONFLICT_MARKER = "conflict-marker"

#: The kind recorded for a path the index still holds as unmerged.
KIND_UNMERGED_ENTRY = "unmerged-index-entry"

#: The kind recorded for a path that is not tracked yet but already
#: looks like debris -- one blanket ``git add`` away from the tracked
#: tree.
KIND_PENDING_RESIDUE = "pending-residue-path"

#: The kind recorded for a file whose whole content is patch text.
KIND_PATCH_TEXT = "patch-text"

#: The line prefixes that make a file nothing but a patch. A unified
#: diff opens with a hunk header; a git diff opens with a file header.
#: Only the *first* non-empty line is looked at -- see
#: :func:`patch_text_marker`.
PATCH_TEXT_PREFIXES: tuple[str, ...] = ("@@ ", "diff --git ")


@dataclass(frozen=True)
class Residue:
    """One piece of merge debris found in the tracked tree."""

    path: str
    kind: str
    detail: str

    def describe(self) -> str:
        """A one-line report of this finding."""
        return f"{self.path}: {self.kind} ({self.detail})"


@dataclass(frozen=True)
class IndexEntry:
    """One record of the index: a path, its blob, its mode and its stage.

    The stage is the part that carries the merge state. Stage ``0`` is
    an ordinary tracked file; stages ``1``, ``2`` and ``3`` are the
    common ancestor, ours and theirs of a merge that was started and
    never finished.

    The mode says what the entry holds. A regular file is ``100644``
    or ``100755``, a symlink is ``120000``, and a submodule is
    ``160000`` -- a gitlink, whose content is a commit rather than a
    file, so there is nothing to read.
    """

    path: str
    blob_id: str
    stage: int
    mode: str = "100644"


@dataclass(frozen=True)
class BatchRecord:
    """One record of a ``git cat-file --batch`` stream.

    The type matters as much as the payload: the index holds a
    submodule's *commit*, not a blob, and a reader that assumed every
    record was a blob would step over that body and read every file
    after it wrong.
    """

    obj_type: str
    payload: bytes | None


def parse_batch_header(header: str) -> tuple[str, int] | None:
    """Return ``(object type, payload size)`` for a batch header.

    ``None`` means the header announces an object git does not have --
    ``<oid> missing``. Such a record carries no body, so the next
    header follows it immediately and nothing has to be consumed.

    A header that is neither of those two shapes is a fault in the
    reader rather than a fact about the tree: without the size, the
    body cannot be stepped over. It raises instead of returning a
    guess, because a wrong guess desynchronises every record after it
    and the tree is then read wrong without a word.
    """
    fields = header.rsplit(" ", 2)
    if len(fields) == 2 and fields[1] == "missing":
        return None
    if len(fields) != 3 or not fields[2].isdigit():
        raise ValueError(f"git cat-file --batch: unrecognised header {header!r}")
    return fields[1], int(fields[2])


def read_batch_record(
    header: str,
    read: Callable[[int], bytes],
) -> BatchRecord | None:
    """Read one record of a ``git cat-file --batch`` stream.

    *read* is called with a byte count and must return exactly that
    many bytes. The record's trailing newline is consumed as well, so
    the next read starts on the next header -- whatever this record's
    type was. ``None`` is returned for a header that announces no
    body.
    """
    parsed = parse_batch_header(header)
    if parsed is None:
        return None
    obj_type, size = parsed
    payload = read(size)
    read(1)  # the newline that ends the record
    return BatchRecord(obj_type=obj_type, payload=payload)


def residue_path_rule(path: str) -> str | None:
    """Return the rule that makes *path* look like debris, or ``None``.

    The returned string names the rule that fired, so a report says
    *why* a path was flagged and not only that it was.

    The rules are tried in the order they are listed, and the first
    one that fires names the finding. The two shape rules come last on
    purpose: a path that already carries the ``diff against`` marker
    keeps that name, because the marker says more about the path than
    the whitespace it happens to sit in.
    """
    lowered = path.lower()
    for marker in RESIDUE_PATH_MARKERS:
        if marker in lowered:
            return marker
    for suffix in RESIDUE_PATH_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    if any(char.isspace() for char in path):
        return RULE_PATH_WHITESPACE
    if not path.isascii():
        return RULE_PATH_NON_ASCII
    return None


def conflict_markers(text: str) -> list[str]:
    """Return the conflict markers *text* carries, in order of appearance.

    A marker counts only when it starts a line: git writes markers in
    column zero, and prose that names them is not a conflict. Each
    occurrence is reported once per line it appears on, so a file with
    two blocks reports two start markers.

    The separator is counted only as part of a block. A line of seven
    equals signs on its own is a section rule that source files use,
    and it is a conflict only when the block's opening or closing
    marker is there too.
    """
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(CONFLICT_START):
            found.append(CONFLICT_START)
        elif stripped.startswith(CONFLICT_END):
            found.append(CONFLICT_END)
        elif stripped == CONFLICT_SEPARATOR:
            found.append(CONFLICT_SEPARATOR)
    if not any(marker in (CONFLICT_START, CONFLICT_END) for marker in found):
        return []
    return found


def find_unmerged_entries(entries: Sequence[IndexEntry]) -> list[Residue]:
    """Return the paths the index still holds as unmerged.

    A merge that was started and not finished leaves the same path in
    the index at more than one stage: the common ancestor, ours and
    theirs. Nothing is committed from such a path, and the tree the
    next commit would carry is one nobody wrote -- so a tracked tree
    that still holds one is exactly the merge debris this gate is
    about.

    The result is sorted by path, and the stages a path is held at are
    reported together, so an unfinished merge is one finding rather
    than three.
    """
    stages: dict[str, set[int]] = {}
    for entry in entries:
        if entry.stage != 0:
            stages.setdefault(entry.path, set()).add(entry.stage)
    residues: list[Residue] = []
    for path in sorted(stages):
        held = ", ".join(str(stage) for stage in sorted(stages[path]))
        residues.append(
            Residue(
                path=path,
                kind=KIND_UNMERGED_ENTRY,
                detail=f"stages {held}",
            )
        )
    return residues


def find_pending_residue(untracked: Sequence[str]) -> list[Residue]:
    """Return the untracked paths that would become debris once staged.

    An untracked file is one blanket ``git add`` away from the tracked
    tree. One that already looks like debris is therefore debris in
    waiting -- and the blanket add is the framework's checkpoint
    commit, which runs *after* this suite.

    The rule is the name rule of :func:`find_merge_residue`: the file
    is not read, only named. A path whose name is already debris is
    debris whether or not the index has it yet.
    """
    residues: list[Residue] = []
    for path in untracked:
        rule = residue_path_rule(path)
        if rule is None:
            continue
        residues.append(
            Residue(path=path, kind=KIND_PENDING_RESIDUE, detail=rule)
        )
    residues.sort(key=lambda residue: (residue.path, residue.kind, residue.detail))
    return residues


def find_path_residue(paths: Sequence[str]) -> list[Residue]:
    """Return the debris found in the *names* of *paths*.

    A path whose name says what it is -- a diff fragment, a rejected
    patch -- is debris whatever its content says.
    """
    residues: list[Residue] = []
    for path in paths:
        rule = residue_path_rule(path)
        if rule is not None:
            residues.append(
                Residue(path=path, kind=KIND_RESIDUE_PATH, detail=rule)
            )
    residues.sort(key=lambda residue: (residue.path, residue.kind, residue.detail))
    return residues


def patch_text_marker(text: str) -> str | None:
    """Return the patch header *text* opens with, or ``None``.

    A file whose first non-empty line is a patch header is a patch, not
    a source file: nothing in this project starts with a hunk header.
    Only the **first** non-empty line counts -- a source file may quote
    a patch in a docstring or a fixture, and a gate that flagged any
    hunk header in the file would cry wolf on the very tests that
    describe this rule.
    """
    for line in text.splitlines():
        if not line.strip():
            continue
        for prefix in PATCH_TEXT_PREFIXES:
            if line.startswith(prefix):
                return prefix.rstrip()
        return None
    return None


def find_content_residue(contents: Mapping[str, str]) -> list[Residue]:
    """Return the debris found in the *contents* of the given files.

    Kept apart from the name rule on purpose: the same content rules
    are applied to two different texts -- the blob the index holds and
    the bytes the working tree holds -- and a caller has to be able to
    judge the second without the first being judged twice.
    """
    residues: list[Residue] = []
    for path, text in contents.items():
        for marker in conflict_markers(text):
            residues.append(
                Residue(path=path, kind=KIND_CONFLICT_MARKER, detail=marker)
            )
        patch = patch_text_marker(text)
        if patch is not None:
            residues.append(
                Residue(path=path, kind=KIND_PATCH_TEXT, detail=patch)
            )
    residues.sort(key=lambda residue: (residue.path, residue.kind, residue.detail))
    return residues


def find_merge_residue(
    paths: Sequence[str],
    contents: Mapping[str, str] | None = None,
) -> list[Residue]:
    """Return every piece of merge debris in *paths* and *contents*.

    *paths* is the list of tracked paths; *contents* maps a path to the
    text of that file. A path with no entry in *contents* is judged by
    its name alone -- the caller decides which files are worth reading.

    The result is sorted by path, then kind, then detail, so two runs
    over the same tree produce the same list in the same order.
    """
    residues = find_path_residue(paths) + find_content_residue(contents or {})
    residues.sort(key=lambda residue: (residue.path, residue.kind, residue.detail))
    return residues


def is_text_entry(path: str) -> bool:
    """Return True iff the file at *path* is worth reading for markers.

    The extension decides, and a path with **no** extension counts as
    text: ``.gitignore``, ``LICENSE``, ``.coveragerc`` and ``.bandit``
    are all extensionless, and a conflict marker left in one of them
    is just as much debris as one in a module. Judging by extension
    alone left six tracked files unread.

    Documentation formats are left out on purpose: prose that quotes a
    conflict block is a document, not debris.
    """
    suffix = Path(path).suffix.lower()
    return suffix == "" or suffix in TEXT_EXTENSIONS


def unread_text_entries(
    entries: Sequence[IndexEntry],
    contents: Mapping[str, str],
) -> list[str]:
    """Return the text entries whose content the reader did not deliver.

    A reader that silently drops files judges a fraction of the tree
    and passes the gate anyway -- and it passes it in the direction
    that lets debris through. The gap has to be named, so it is
    returned as a list rather than absorbed.

    An entry that cannot have content is not expected to deliver any:
    a submodule's gitlink names a commit, and an unmerged path has no
    single content to judge. Both are reported as debris in their own
    right.
    """
    wanted = {
        entry.path
        for entry in entries
        if entry.stage == 0
        and entry.mode != GITLINK_MODE
        and is_text_entry(entry.path)
    }
    return sorted(wanted - set(contents))


def is_readable_working_tree_file(root: Path, path: str) -> bool:
    """Return True iff *path* is a regular file that may be read.

    A symlink is not: its own content is the target path, and reading
    through the link would judge a file the tree does not hold and
    report that file's markers against the link's path.
    """
    target = root / path
    return not target.is_symlink() and target.is_file()


def working_tree_contents(
    paths: Sequence[str],
    root: Path = REPO_ROOT,
) -> dict[str, str]:
    """Return the on-disk content of every path in *paths* that is a file.

    The index says what is committed; the working tree says what the
    next blanket ``git add`` will take. Those are different texts: a
    merge left unresolved on disk is invisible to the index, which
    still holds the pre-merge blob, and fatal to the commit that
    follows. Both are read, and both are judged.

    A path that is not a regular file on disk -- deleted, a symlink --
    is skipped: there is no content to read there.
    """
    contents: dict[str, str] = {}
    for path in paths:
        if not is_readable_working_tree_file(root, path):
            continue
        try:
            contents[path] = (root / path).read_bytes().decode("utf-8", "replace")
        except OSError:
            # The coverage check below names the file if it is on disk
            # and was not delivered -- a read that fails is not a
            # reason to judge less of the tree silently.
            continue
    return contents


def unread_working_tree_entries(
    entries: Sequence[IndexEntry],
    contents: Mapping[str, str],
    root: Path = REPO_ROOT,
) -> list[str]:
    """Return the text entries that are on disk but were not read.

    A file that exists in the working tree and was not delivered by the
    reader is a gap in the read, not a deletion -- the difference is
    the point. A path that is gone from the working tree is not
    expected there: the index still holds it, and the index is judged
    on its own.
    """
    wanted = {
        entry.path
        for entry in entries
        if entry.stage == 0
        and entry.mode != GITLINK_MODE
        and is_text_entry(entry.path)
    }
    missing = [
        path
        for path in sorted(wanted)
        if path not in contents
        and is_readable_working_tree_file(root, path)
    ]
    return missing


def tracked_index_entries() -> list[IndexEntry]:
    """Return one :class:`IndexEntry` for every record in the index.

    ``git ls-files -s`` lists the index: the mode, the blob id, the
    stage and the path, one record per entry. ``-z`` makes the records
    NUL separated, so a path holding a space or a bracket survives the
    trip in one piece -- and the debris this gate looks for has exactly
    such a name.

    The stage is kept because it carries the merge state: a path held
    at a stage other than ``0`` is a merge that was never finished.
    The mode is kept because it says what the entry holds: a file to
    be read, or a gitlink whose content is a commit.
    """
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", "-z"],
        capture_output=True, text=True, check=True,
    )
    entries: list[IndexEntry] = []
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        # "<mode> SP <blob id> SP <stage> TAB <path>"
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or not path:
            continue
        try:
            stage = int(fields[2])
        except ValueError:
            continue
        entries.append(
            IndexEntry(path=path, blob_id=fields[1], stage=stage, mode=fields[0])
        )
    return entries


def untracked_paths() -> list[str]:
    """Return every path in the working tree that the index does not hold.

    ``git ls-files --others --exclude-standard`` lists them: files git
    neither tracks nor ignores. ``-z`` separates the records, so a path
    holding a space or a bracket survives in one piece -- and the
    debris this gate looks for has exactly such a name.

    They are read from the *working tree* rather than the index on
    purpose. The index says what is tracked now; this says what would
    be tracked after the next blanket ``git add`` -- and the framework
    takes its checkpoint commit with exactly such an add, after this
    suite has run. A residue file waiting here would pass an
    index-only gate and land in the commit anyway.
    """
    completed = subprocess.run(
        [
            "git", "-C", str(REPO_ROOT),
            "ls-files", "--others", "--exclude-standard", "-z",
        ],
        capture_output=True, text=True, check=True,
    )
    return [record for record in completed.stdout.split("\0") if record]


def index_blob_contents(entries: Sequence[IndexEntry]) -> dict[str, str]:
    """Return the index content of every entry that is a text file.

    The blob ids come from the index and ``git cat-file --batch`` hands
    back the bytes of each one, so what is judged is what the next
    commit would carry. Entries that are not blobs (a submodule's
    commit) and entries whose blob cannot be read are skipped.

    Only resolved entries (stage ``0``) are read: an unmerged path has
    no single content to judge, and it is reported as debris in its own
    right.
    """
    wanted = [
        (entry.path, entry.blob_id)
        for entry in entries
        if entry.stage == 0
        and entry.mode != GITLINK_MODE
        and is_text_entry(entry.path)
    ]
    if not wanted:
        return {}
    process = subprocess.Popen(
        ["git", "-C", str(REPO_ROOT), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:  # pragma: no cover
        process.kill()
        process.wait()
        return {}
    contents: dict[str, str] = {}
    try:
        for path, blob_id in wanted:
            process.stdin.write(blob_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("utf-8", "replace").strip()
            record = read_batch_record(header, process.stdout.read)
            if record is None or record.payload is None:
                continue
            if record.obj_type != "blob":
                # The body was consumed above whatever its type was.
                # Skipping it instead would leave the next header read
                # sitting on the body, and every file after it would
                # be read wrong -- silently, and in the direction that
                # lets debris through.
                continue
            contents[path] = record.payload.decode("utf-8", "replace")
    finally:
        process.stdin.close()
        process.wait()
    return contents


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_tracked_tree_carries_no_merge_residue() -> None:
    """No tracked path and no tracked source file may hold merge debris."""
    entries = tracked_index_entries()
    assert entries, (
        "git ls-files returned no entry -- the gate would now pass on an "
        "empty tree."
    )
    paths = sorted({entry.path for entry in entries})
    contents = index_blob_contents(entries)
    assert contents, (
        "no index content was read -- the marker half of this gate would "
        "pass without having looked at a single file."
    )
    unread = unread_text_entries(entries, contents)
    assert unread == [], (
        "these tracked text files were not read, so the marker half of "
        "this gate judged only part of the tree:\n  "
        + "\n  ".join(unread[:20])
    )
    worktree_contents = working_tree_contents(
        [path for path in paths if is_text_entry(path)]
    )
    unread_worktree = unread_working_tree_entries(entries, worktree_contents)
    assert unread_worktree == [], (
        "these tracked text files are on disk but were not read, so the "
        "working-tree half of this gate judged only part of the tree:\n  "
        + "\n  ".join(unread_worktree[:20])
    )
    residue = (
        find_merge_residue(paths, contents)
        + find_content_residue(worktree_contents)
        + find_unmerged_entries(entries)
    )
    assert residue == [], (
        "merge-resolution residue is tracked:\n  "
        + "\n  ".join(item.describe() for item in residue)
    )


def test_no_untracked_file_is_one_add_away_from_the_tracked_tree() -> None:
    """No residue file may be waiting in the working tree to be staged.

    The gate above reads the index, and the framework's checkpoint
    commit -- a blanket ``git add`` -- runs *after* this suite. A
    residue file sitting in the working tree right now is one blanket
    add away from the tracked tree, and an index-only gate cannot see
    it. That is how the diff fragment came back after it had already
    been removed: nothing tracked it, it was merely waiting.
    """
    residue = find_pending_residue(untracked_paths())
    assert residue == [], (
        "merge-resolution residue is waiting in the working tree to be "
        "staged:\n  " + "\n  ".join(item.describe() for item in residue)
    )


# ---------------------------------------------------------------------------
# Sensitivity -- the rules, exercised with synthetic inputs
# ---------------------------------------------------------------------------


def test_a_diff_against_path_is_debris() -> None:
    """The shape that reached the tree is caught by name."""
    path = "backend/verification/orchestrator.py (diff against `abc1234`)"
    assert residue_path_rule(path) == "diff against"


def test_orig_and_rej_paths_are_debris() -> None:
    """git's own conflict leftovers are caught by suffix."""
    assert residue_path_rule("backend/verification_loop.py.orig") == ".orig"
    assert residue_path_rule("backend/verification_loop.py.rej") == ".rej"


def test_an_ordinary_path_is_not_debris() -> None:
    """A source file that merely ends in ``.py`` is not flagged."""
    assert residue_path_rule("backend/verification/orchestrator.py") is None


def test_a_path_with_whitespace_is_debris() -> None:
    """A tracked name that carries a space is prose that landed in the tree.

    The shape that reached the tree was not a file *name* but a
    *description* of one -- ``test_foo.py （修改，1070 行）``. A space in
    a tracked path is how that shape reads, whichever script wrote it,
    and no extension rule reaches it: the suffix of such a path is the
    whole tail of the prose.
    """
    path = (
        "backend/tests/static_gates/test_frozen_hash_refresh_contract.py "
        "（新建，135 行，全文见下）"
    )
    assert residue_path_rule(path) == "path-with-whitespace"


def test_a_path_with_non_ascii_characters_is_debris() -> None:
    """The same shape, caught by the characters it is written in.

    A tracked path whose name carries a non-ASCII character is a
    description in the author's own script, not a file name the project
    chose.
    """
    assert residue_path_rule("backend/tests/static_gates/测试副本.py") == "non-ascii-path"


def test_the_diff_against_marker_still_names_its_own_shape() -> None:
    """The shape rules are *additional* -- the marker keeps its name.

    The path that carries a ``diff against`` marker also carries
    whitespace, and the finding for it must still say *why* it was
    flagged: the marker, not the space the marker happens to sit in.
    """
    path = "backend/verification/orchestrator.py (diff against `abc1234`)"
    assert residue_path_rule(path) == "diff against"


def test_a_conflict_block_is_found_in_a_file() -> None:
    """Every marker line of a block is reported, in order."""
    sample = "\n".join([
        "before",
        CONFLICT_START + " HEAD",
        "ours",
        "=" * 7,
        "theirs",
        CONFLICT_END + " feature",
        "after",
    ])
    assert conflict_markers(sample) == [
        CONFLICT_START,
        CONFLICT_SEPARATOR,
        CONFLICT_END,
    ]


def test_a_separator_line_alone_is_not_a_conflict() -> None:
    """A section rule that happens to be seven equals signs is prose.

    Source files use a line of equals signs to divide sections. It is a
    conflict only when the block's opening or closing marker is there
    too, and reporting it on its own would make the gate cry wolf on
    ordinary source files.
    """
    sample = "\n".join([
        "def run() -> None:",
        "    " + "=" * 7,
        "    pass",
    ])
    assert conflict_markers(sample) == []


def test_a_marker_mentioned_mid_line_is_not_a_conflict() -> None:
    """Prose that names the markers is not debris.

    The repository has a module that defines the marker string it
    searches for, and a test that asserts on one. A gate that flagged
    either would flag the code that exists to detect conflicts.
    """
    sample = 'CONFLICT_MARKER = "' + CONFLICT_START + ' "'
    assert conflict_markers(sample) == []


def test_a_file_that_is_patch_text_is_residue() -> None:
    """A file whose whole content is a patch is debris by its content.

    The gate's own description names this shape: a file that is a patch
    against another revision, sitting next to the file it describes. Its
    first non-empty line opens a unified-diff hunk header, which is not
    a thing any source file in this project starts with.
    """
    sample = "\n".join([
        "@@ -654,11 +654,16 @@ FROZEN_R10_SURFACE_SHA256: dict[str, str] = {",
        " context",
        "-old",
        "+new",
    ])
    residue = find_content_residue({"backend/verification/orchestrator.py": sample})
    assert [(item.kind, item.detail) for item in residue] == [
        (KIND_PATCH_TEXT, "@@"),
    ]


def test_a_file_that_is_a_git_diff_is_residue() -> None:
    """The other patch header: a git-style diff, opened by its file line."""
    sample = "\n".join([
        "diff --git a/backend/verification_loop.py b/backend/verification_loop.py",
        "index 1111111..2222222 100644",
        "--- a/backend/verification_loop.py",
    ])
    residue = find_content_residue({"backend/verification_loop.py": sample})
    assert [(item.kind, item.detail) for item in residue] == [
        (KIND_PATCH_TEXT, "diff --git"),
    ]


def test_a_source_file_is_not_patch_text() -> None:
    """An ordinary source file is not debris, however it is written."""
    sample = "def run() -> None:\n    pass\n"
    assert find_content_residue({"backend/verification/orchestrator.py": sample}) == []


def test_a_hunk_header_further_down_is_not_patch_text() -> None:
    """Only the *first* non-empty line decides.

    A source file may quote a patch -- in a docstring, in a fixture. A
    gate that flagged any hunk header in the file would cry wolf on the
    very tests that describe this rule, so the header has to open the
    file to count.
    """
    sample = "\n".join([
        "def run() -> None:",
        '    """See the patch below."""',
        "    # @@ -1,3 +1,4 @@",
    ])
    assert find_content_residue({"backend/verification/orchestrator.py": sample}) == []


def test_find_merge_residue_reports_both_kinds() -> None:
    """A path flagged by name and by content reports both findings."""
    path = "backend/verification/orchestrator.py (diff against `abc1234`)"
    sample = "\n".join([
        CONFLICT_START + " HEAD",
        "ours",
        CONFLICT_END + " feature",
    ])
    residue = find_merge_residue([path], {path: sample})
    assert [(item.kind, item.detail) for item in residue] == [
        (KIND_CONFLICT_MARKER, CONFLICT_START),
        (KIND_CONFLICT_MARKER, CONFLICT_END),
        (KIND_RESIDUE_PATH, "diff against"),
    ]


def test_find_merge_residue_is_empty_for_a_clean_tree() -> None:
    """A tree without debris reports nothing."""
    paths = ["backend/verification_loop.py", "backend/verification_agent.py"]
    contents = {
        "backend/verification_loop.py": "def run() -> None:\n    pass\n",
        "backend/verification_agent.py": "def judge() -> None:\n    pass\n",
    }
    assert find_merge_residue(paths, contents) == []


def test_an_unmerged_index_entry_is_residue() -> None:
    """A path the index still holds at a merge stage is debris.

    A merge that was started and not finished leaves the same path in
    the index at more than one stage: the common ancestor, ours and
    theirs. Nothing is committed from such a path, and the tree the
    next commit would carry is one nobody wrote.
    """
    entries = [
        IndexEntry(path="backend/verification_loop.py", blob_id="a" * 40, stage=1),
        IndexEntry(path="backend/verification_loop.py", blob_id="b" * 40, stage=2),
        IndexEntry(path="backend/verification_loop.py", blob_id="c" * 40, stage=3),
    ]
    assert [
        (item.path, item.kind, item.detail)
        for item in find_unmerged_entries(entries)
    ] == [
        ("backend/verification_loop.py", KIND_UNMERGED_ENTRY, "stages 1, 2, 3"),
    ]


def test_a_resolved_index_entry_is_not_unmerged() -> None:
    """A stage-0 entry is an ordinary tracked file."""
    entries = [
        IndexEntry(path="backend/verification_loop.py", blob_id="a" * 40, stage=0),
    ]
    assert find_unmerged_entries(entries) == []


# ---------------------------------------------------------------------------
# The batch reader -- the stream must not shift under a non-blob record
# ---------------------------------------------------------------------------


def test_a_batch_header_names_the_type_and_the_size() -> None:
    """A header carries the object type and the size of its body."""
    assert parse_batch_header("a" * 40 + " blob 5") == ("blob", 5)
    assert parse_batch_header("a" * 40 + " commit 4") == ("commit", 4)


def test_a_missing_object_has_no_body() -> None:
    """``<oid> missing`` announces an object git does not have.

    Such a record carries no body, so the next header follows it
    immediately and nothing has to be consumed.
    """
    assert parse_batch_header("a" * 40 + " missing") is None


def test_a_non_blob_record_does_not_shift_the_next_record() -> None:
    """A record that is not a blob still has a body, and it is consumed.

    A submodule's entry is a commit, not a blob. A reader that skipped
    such a body instead of reading it would leave the next header read
    sitting on the body -- and every file after it would then be read
    wrong, without a word. The gate would still pass, on a tree it had
    only partly read.
    """
    stream = io.BytesIO(b"tree\n" + b"hello\n")
    first = read_batch_record("a" * 40 + " commit 4", stream.read)
    assert first is not None
    assert (first.obj_type, first.payload) == ("commit", b"tree")
    second = read_batch_record("b" * 40 + " blob 5", stream.read)
    assert second is not None
    assert (second.obj_type, second.payload) == ("blob", b"hello")


def test_a_missing_object_leaves_the_stream_where_it_was() -> None:
    """A missing object consumes nothing, so the next record still lines up."""
    stream = io.BytesIO(b"hello\n")
    assert read_batch_record("a" * 40 + " missing", stream.read) is None
    record = read_batch_record("b" * 40 + " blob 5", stream.read)
    assert record is not None
    assert record.payload == b"hello"


def test_an_unreadable_header_is_refused_rather_than_guessed() -> None:
    """A header whose size is unknown cannot be stepped over.

    Returning a guess would desynchronise every record after it, and
    the tree would then be read wrong without a word. Raising keeps the
    failure where it belongs: in the reader.
    """
    with pytest.raises(ValueError):
        parse_batch_header("a" * 40 + " blob")


# ---------------------------------------------------------------------------
# The working tree -- debris that is waiting to be staged
# ---------------------------------------------------------------------------


def test_an_untracked_residue_file_is_debris_one_add_away() -> None:
    """The rule the untracked half of the gate applies, on synthetic input.

    The untracked half judges by name alone: it does not read the
    file, it only asks what the file is called. A path whose name is
    already debris is debris whether or not the index has it yet.
    """
    untracked = ["backend/verification/orchestrator.py (diff against `abc1234`)"]
    assert [
        (item.path, item.kind, item.detail)
        for item in find_pending_residue(untracked)
    ] == [
        (
            "backend/verification/orchestrator.py (diff against `abc1234`)",
            KIND_PENDING_RESIDUE,
            "diff against",
        ),
    ]


def test_an_untracked_ordinary_file_is_not_pending_debris() -> None:
    """A source file that is merely not tracked yet is not flagged."""
    assert find_pending_residue(["backend/verification/orchestrator.py"]) == []


def test_the_pending_findings_are_ordered_by_path() -> None:
    """Two runs over the same tree produce the same list in the same order."""
    untracked = [
        "backend/verification_loop.py.rej",
        "backend/verification/orchestrator.py (diff against `abc1234`)",
    ]
    assert [item.path for item in find_pending_residue(untracked)] == [
        "backend/verification/orchestrator.py (diff against `abc1234`)",
        "backend/verification_loop.py.rej",
    ]


# ---------------------------------------------------------------------------
# Coverage -- the gate must judge every text file, and say so when it cannot
# ---------------------------------------------------------------------------


def test_a_file_without_an_extension_is_read_for_markers() -> None:
    """A dotfile or a plain file is text too.

    ``.gitignore``, ``LICENSE``, ``.coveragerc`` and ``.bandit`` carry
    no extension, and a conflict marker left in one of them is just as
    much debris as one in a module. Judging by extension alone left
    six tracked files unread.
    """
    assert is_text_entry(".gitignore")
    assert is_text_entry("LICENSE")
    assert is_text_entry("backend/.bandit")
    assert is_text_entry("backend/verification_loop.py")
    assert not is_text_entry("backend/logo.png")
    assert not is_text_entry("docs/design.md")


def test_a_diff_file_is_read_for_its_content() -> None:
    """The canonical name of the shape this gate exists for is text.

    A diff fragment parked next to the file it describes is the shape
    the gate's own description names first. Its ordinary name is
    ``fix.diff`` or ``fix.patch``, and until that extension counted as
    text the gate never read such a file -- so the patch-text rule
    could not see the one shape the gate was written for.
    """
    assert is_text_entry("backend/fix.diff")
    assert is_text_entry("backend/fix.patch")
    sample = (
        "diff --git a/backend/verification_loop.py b/backend/verification_loop.py\n"
    )
    residue = find_content_residue({"backend/fix.diff": sample})
    assert [(item.kind, item.detail) for item in residue] == [
        (KIND_PATCH_TEXT, "diff --git"),
    ]


def test_a_reader_that_dropped_a_file_is_named() -> None:
    """A partial read is a finding, not a quiet pass.

    A reader that silently drops files judges a fraction of the tree
    and passes the gate anyway -- and it passes it in the direction
    that lets debris through. The gap has to be named.
    """
    entries = [
        IndexEntry(path="a.py", blob_id="a" * 40, stage=0),
        IndexEntry(path="b.py", blob_id="b" * 40, stage=0),
    ]
    assert unread_text_entries(entries, {"a.py": "x"}) == ["b.py"]
    assert unread_text_entries(entries, {"a.py": "x", "b.py": "y"}) == []


def test_a_submodule_entry_is_not_expected_to_produce_content() -> None:
    """A submodule's entry is a commit, not a file to be judged.

    Its path has no extension, so the name rule alone would ask for
    content that a gitlink does not have. The mode says which entries
    hold a file at all.
    """
    entries = [
        IndexEntry(path="vendor/lib", blob_id="c" * 40, stage=0, mode="160000"),
    ]
    assert unread_text_entries(entries, {}) == []


def test_an_unmerged_entry_is_not_expected_to_produce_content() -> None:
    """An unmerged path has no single content to judge.

    It is reported as debris in its own right, so the reader is not
    asked for a body it cannot have.
    """
    entries = [
        IndexEntry(path="a.py", blob_id="a" * 40, stage=1),
    ]
    assert unread_text_entries(entries, {}) == []


# ---------------------------------------------------------------------------
# The working tree -- the content the next commit actually carries
# ---------------------------------------------------------------------------


def test_the_working_tree_is_read_for_markers(tmp_path) -> None:
    """The content the next commit carries is the one on disk.

    The framework's checkpoint commit is a blanket ``git add``: it takes
    what is on disk, not what the index holds. A tracked file whose
    working-tree content carries a conflict marker is therefore
    committed as debris while a gate that reads only the index stays
    green -- the index still holds the pre-merge blob.
    """
    (tmp_path / "a.py").write_text("clean\n", encoding="utf-8")
    assert working_tree_contents(["a.py"], root=tmp_path) == {"a.py": "clean\n"}


def test_a_conflict_marker_in_the_working_tree_is_found_when_the_index_is_clean() -> None:
    """The two halves are read separately, and both are judged.

    The index holds what is committed; the working tree holds what the
    next blanket add will take. A merge left unresolved on disk is
    invisible to the first and fatal to the second, so the content
    rules have to be applied to both.
    """
    index_contents = {"a.py": "print('fine')\n"}
    worktree_contents = {
        "a.py": (
            f"{CONFLICT_START} HEAD\nours\n"
            f"{CONFLICT_SEPARATOR}\ntheirs\n"
            f"{CONFLICT_END} other\n"
        ),
    }
    assert find_content_residue(index_contents) == []
    assert len(find_content_residue(worktree_contents)) == 3


def test_a_symlink_is_not_read_through(tmp_path) -> None:
    """A symlink's own content is its target path, not the target's bytes.

    Reading through the link would judge a file the tree does not hold
    and would report that file's markers against the link's path. The
    index stores the link's target as a string, and the working tree
    has to be read the same way.
    """
    target = tmp_path / "target.txt"
    target.write_text(f"{CONFLICT_START} HEAD\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(target)
    assert working_tree_contents(["link.txt", "target.txt"], root=tmp_path) == {
        "target.txt": f"{CONFLICT_START} HEAD\n",
    }


def test_a_reader_that_dropped_a_working_tree_file_is_named(tmp_path) -> None:
    """A partial read of the working tree is a finding, not a quiet pass.

    The same argument as for the index: a reader that drops files
    judges a fraction of the tree and passes in the direction that lets
    debris through. The gap has to be named.
    """
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y\n", encoding="utf-8")
    entries = [
        IndexEntry(path="a.py", blob_id="a" * 40, stage=0),
        IndexEntry(path="b.py", blob_id="b" * 40, stage=0),
    ]
    contents = working_tree_contents(["b.py"], root=tmp_path)
    assert unread_working_tree_entries(entries, contents, root=tmp_path) == ["a.py"]
    assert unread_working_tree_entries(
        entries, working_tree_contents(["a.py", "b.py"], root=tmp_path), root=tmp_path,
    ) == []


def test_a_deleted_file_is_not_expected_in_the_working_tree(tmp_path) -> None:
    """A path removed from the working tree has no content to read there.

    The index still holds it, and the index is judged on its own, so a
    deletion is not a gap -- it is a deletion.
    """
    entries = [
        IndexEntry(path="gone.py", blob_id="a" * 40, stage=0),
    ]
    assert unread_working_tree_entries(entries, {}, root=tmp_path) == []
