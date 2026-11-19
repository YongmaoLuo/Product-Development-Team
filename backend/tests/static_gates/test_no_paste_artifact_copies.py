"""No pasted copy may sit in the tracked tree.

Why this gate exists
--------------------
A *pasted copy* is a file whose **name** was taken from a message and
written into the source tree verbatim. It is not a broken version of
something; it is a second copy of something, and nothing in the tree says
so except the name — which is exactly what makes it dangerous.

Two of them were committed here, both under ``backend/tests/static_gates/``:

* one was a byte-for-byte paste of the gate module beside it, down to the
  docstring, with only the trailing newline missing;
* one was not Python at all. It was the *diff* between a gate module and
  its previous version, pasted into a file whose name ended with the
  diff's own description. Reading it gives you a file that cannot be
  imported, a diff that applies to nothing, and a module that looks
  present.

A third one sat in ``backend/routes/``: the *diff* of a route module,
pasted into a file whose name also ended with the diff's own
description. Its body is a hunk and nothing else.

Neither reads anything. Neither is imported. Both are in the tracked set,
so every later ``import``, every later static gate, every later audit
that walks the tree has to decide what to do with them — and the answer
"skip it, it is obviously a copy" is a decision a machine has to make
from a filename.

The first repair deleted them by name. That is not a fix, because the
commit that introduced them (``e8b568f4``) is an ancestor of the base
branch: the moment ``origin/main`` is merged back in, both files return,
byte for byte, and the repository is polluted again. A cleanup that has
to be repeated after every merge is a cleanup that has to be *enforced*.

So this is a gate, not a cleanup.

What it pins
------------
Two rules about the **name** of a tracked path:

1. **The marker rule.** The name carries a word that only a pasted copy
   would carry — a note that the file is new or modified, that the file
   is a complete diff, or that the file is the complete contents of
   something else. This rule applies to every tracked path in the
   repository, because a file named ``（完整 diff）`` is a copy whether it
   sits in ``backend/`` or in ``docs/``.
2. **The structural rule.** The name is a legal source extension,
   followed by whitespace, followed by a full-width left parenthesis.
   This is the shape a copy takes when it is *named* rather than
   described, and it is checked under the source roots —
   ``backend/``, ``frontend/``, ``scripts/`` and ``tools/`` — because
   that is where a copy of a source file lands and where a reader would
   mistake it for one.

Both of those read names, and a copy whose name was cleaned up before it
was committed — ``verification_v2.py``, ``verification_helper.py`` — has
no marker word and no parenthesis to find. So three more rules are
pinned here, and they read the tree rather than the names:

3. **The uniqueness rule.** ``backend/routes/verification.py`` is the
   only module that serves the verification run API. The question is
   asked of the *surface* rather than of the name: which tracked modules
   declare a route under ``/api/verification/``. A second module that
   declares one is a second implementation of ``routes.verification``,
   whatever its name happens to be. The neighbour that must not be
   counted is ``backend/routes/phases.py``, which serves
   ``/api/verification-plan/...`` — the *plan* of a verification phase,
   a different surface.
4. **The source rule.** The modules the deleted copies were pasted from
   are still tracked. A copy may be deleted; the module it copied may
   not, because a deleted source is a loss and not a cleanup.
5. **The body rule.** A tracked file whose **body** opens with the
   header line of a unified-diff hunk — ``@@ -a,b +c,d @@`` — is a
   pasted diff fragment, whatever its name is. This is the rule that
   catches the copy whose name was cleaned up before it was committed:
   the name says nothing, and the body is the only thing left that says
   what the file is. The header of a *whole* patch — ``diff --git``,
   ``--- a/...``, ``+++ b/...`` — is not that: a patch file carries a
   header, and a pasted fragment is the hunk alone.

Why the rules are not a blacklist of known offenders
----------------------------------------------------
A rule that names the two files that exist today catches those two files
and nothing else: the next pasted copy arrives with a different name and
walks past. So the rules are written over **shapes**, and the two
historical names are kept only as *samples* — a check that the shape rule
still recognises a name that really was committed, rather than a name
invented for the test.

That is why :func:`is_paste_artifact_path` is a separate pure function
with the shape rules in it and the sample list outside it: the samples
can be wrong without the rule being wrong, and the rule can be tested
against names nobody chose on purpose.

The samples are also the reason this gate can be shown to be neither
always-true nor always-false. A rule that fires on everything is a rule
that has stopped being a rule, and it fails on the first legitimate file
somebody adds; a rule that fires on nothing is a rule that documents an
intention. Both failure modes are pinned by tests here.

Why ``HEAD`` and not the working tree
-------------------------------------
The same reason ``test_every_tracked_directory_is_scanned`` compares
against the commit: a file that exists on disk but is not committed is
not something the repository publishes, and a gate that fired on it would
fail for the ordinary reason of "a change is in progress". The commit is
the moment an unread copy becomes a published one, so the commit is the
moment this checks.

The gate reads and nothing else: ``git ls-tree`` for the tracked set,
``git grep`` and ``git cat-file`` for the declarations, no writes, no
network, and the same answer on every run.

Why the scan can be pointed at another commit
---------------------------------------------
``HEAD`` is the tree the gate runs over, and it is not the only tree it
can read. The three read functions — the tracked set, the blob bodies
and the declaration scan — take the tree to read as an argument, and
``HEAD`` is the default.

The reason they can is the historical claim. "This rule catches the
copies that were really committed to this repository" is a statement
about one commit, and a rule quietly narrowed to the current tree would
satisfy every test written against the current tree while that
statement rotted. So the scan is pointed at that commit here as well,
and asked the same two questions it is asked over the current tree:
three names, and two bodies. The answers are pinned, and so is the size
of the tracked set that produced them — the current tree holds more
paths than that commit does, so a scan that read the wrong tree would
answer with nothing at all.

Scope, stated honestly
----------------------
The two name rules read **names**. They do not compare contents, so
they cannot tell a pasted copy from a legitimate file that happens to
carry the same name. What they do pin is that a file carrying a name
that only a copy carries cannot be committed, which is the point at
which the copy would otherwise enter the tracked set unnoticed.

The body rule reads **contents**, and it is narrower than it looks: it
only sees a body that *opens* with a hunk header. A pasted **full
text** — the third historical copy, whose body is a whole module —
does not open with one, so the body rule cannot see it and the name
rule is what catches it. The two rules are complements, and neither
replaces the other.

The body rule is asked of two kinds of file: every tracked file whose
extension could hold a pasted body, and every file the name rule has
already flagged. The second is not redundant. A copy whose name says
"here is the complete diff" is precisely the file whose body *is* the
diff, and its name ends in a note rather than in an extension — so the
extension filter on its own would never read it, and the copy would be
named but never examined.

The uniqueness rule reads **declarations** — the routes a module
declares — and so it is narrower than a name rule and states its own
blind spot: a module that builds its path at runtime, rather than
writing it out, is invisible to it. The source rule reads the tracked
set and says nothing at all about contents; it pins that the modules
the copies were pasted from are still there.

Neither the names nor the declarations are read from the working tree.
``git ls-tree`` and ``git grep`` both read the commit, so the answer
does not depend on what happens to be checked out.

Why this module has a command line
-----------------------------------
The rules above are a *gate*: they run in ``pytest`` and in CI, and a
pasted copy is found once the commit that carries it has been made.
That is one stage too late — the copy is in the history by then, and
the cost of taking it out is a rewrite.

So the same rules also run as a command, against the **index**: the
files staged for the next commit, with the bodies they are staged
with. A pasted copy that is still a file the developer can edit is
refused before the commit exists.

The command is here, in the module that owns the rules, and not in a
second script that would have to restate them. The wrapper
``scripts/check_no_paste_artifacts.py`` exists only to put the backend
package on ``sys.path`` and forward the call, so there is one
definition of what a pasted copy is and one place it is written down.

Why this file may be imported without pytest
--------------------------------------------
That wrapper runs under a bare ``python3`` from the pre-commit hook —
no venv, no third-party packages — so this module has to import
without pytest installed. The import below is therefore optional; the
three places that use pytest sit inside test bodies, which only run
when pytest is the one running them.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:  # pragma: no cover - see "Why this file may be imported without pytest"
    import pytest
except ModuleNotFoundError:  # pragma: no cover - the pre-commit hook runs bare
    pytest = None  # type: ignore[assignment]

#: Repository root. This file lives at
#: ``backend/tests/static_gates/``, so the root is three parents up.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: The source roots a pasted copy of a source file would land in. The
#: structural rule is checked under these and nowhere else: they are
#: where the source lives, and where a copy of it would be read as one.
SOURCE_ROOTS: tuple[str, ...] = (
    "backend/",
    "frontend/",
    "scripts/",
    "tools/",
)

#: The extensions a pasted copy of a source file carries. Deliberately
#: the source extensions the repository actually uses — a name ending in
#: ``.py`` and then a parenthesis is a copy of a module, and a name
#: ending in ``.txt`` and then a parenthesis is a note.
#:
#: Markdown and configuration are here for the same reason the rest are:
#: they are pasted too. A note written out as markdown and a workflow
#: written out as YAML are copies of documents, and a copy of a
#: document is exactly what this gate is about.
PASTEABLE_EXTENSIONS: tuple[str, ...] = (
    "py",
    "js",
    "mjs",
    "ts",
    "tsx",
    "json",
    "css",
    "html",
    "sh",
    "md",
    "yml",
    "yaml",
    "txt",
)

#: The structural shape of a named copy: a legal source extension,
#: whitespace, then a full-width left parenthesis. The parenthesis is
#: full-width on purpose — it is the character a Chinese-language
#: message writes, and it is the reason a copy pasted out of such a
#: message does not even parse as a filename on half the tooling.
STRUCTURAL_PASTE_RE: re.Pattern[str] = re.compile(
    r"\.(?:" + "|".join(PASTEABLE_EXTENSIONS) + r")\s+（"
)

#: The words a pasted copy carries in its name. Each one is a note the
#: person pasting wrote *about* the file, addressed to whoever reads the
#: message — never part of a filename anybody chose for a file that is
#: meant to exist.
#:
#: Both the full-width and the half-width parenthesis are accepted: the
#: message that carries the note may be typed either way, and the gate
#: is about the note, not about the keyboard.
MARKER_RE: re.Pattern[str] = re.compile(
    r"(?:"
    r"（modified|\(modified|"
    r"（new|\(new|"
    r"（修改|（新建|"
    r"完整\s*diff|完整内容|全文见下"
    r")"
)

#: The header line of a unified-diff hunk. A pasted diff fragment
#: opens with one; a file that merely *contains* a diff does not. The
#: counts may be omitted — a hunk that covers one line is written
#: ``@@ -1 +1 @@`` — and the trailing section heading may follow.
HUNK_HEADER_RE: re.Pattern[str] = re.compile(
    r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@"
)

#: The three rules this gate enforces, as stable short names. They are
#: what a report says a finding came from, so that a reader is told
#: *which* rule fired rather than only *that* something did. They are
#: named next to the rules they name, and no rule is written down twice.
RULE_NAME_MARKER: str = "name-marker"
RULE_NAME_STRUCTURAL: str = "name-structural"
RULE_BODY_DIFF_FRAGMENT: str = "body-diff-fragment"

#: What each rule says, in one line. A report that names a rule without
#: saying what the rule means makes the reader go and look it up.
RULE_REASONS: dict[str, str] = {
    RULE_NAME_MARKER: (
        "the name carries a marker word that only a pasted copy carries"
    ),
    RULE_NAME_STRUCTURAL: (
        "the name is a source file name followed by a full-width parenthesis"
    ),
    RULE_BODY_DIFF_FRAGMENT: (
        "the body opens with the header line of a unified-diff hunk"
    ),
}

#: The module that implements the verification run API. VP-025's
#: expected_result names it as the only implementation of
#: ``routes.verification``; the rule below is what keeps that true.
VERIFICATION_ROUTES_MODULE: str = "backend/routes/verification.py"

#: The URL prefix the verification run API is served under. The prefix is
#: the discriminator, and it is deliberately the one *with* the trailing
#: slash: ``/api/verification-plan/...`` — the plan of a verification
#: phase, served by ``backend/routes/phases.py`` — is a different surface
#: and must not be counted as a second implementation of this one.
VERIFICATION_ROUTE_PREFIX: str = "/api/verification/"

#: Router factory methods whose first positional argument is the path the
#: route is served under.
ROUTE_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "api_route",
        "add_api_route",
    }
)


def paste_artifact_path_rule(rel_path: str) -> str | None:
    """Return the name rule that flags *rel_path*, or ``None``.

    This is where the two name rules are written down. It is the same
    decision :func:`is_paste_artifact_path` makes — that function is
    the boolean form of this one — so the two cannot disagree about
    what a pasted copy is named.

    *rel_path* is a repository-relative path as ``git ls-tree`` reports
    it. The function is pure: same input, same answer, no filesystem and
    no subprocess — which is what lets the samples below be checked
    without a checkout.
    """
    name = PurePosixPath(rel_path).name
    if MARKER_RE.search(name):
        return RULE_NAME_MARKER
    posix = rel_path.replace("\\", "/")
    if not any(posix.startswith(root) for root in SOURCE_ROOTS):
        return None
    if STRUCTURAL_PASTE_RE.search(name):
        return RULE_NAME_STRUCTURAL
    return None


def is_paste_artifact_path(rel_path: str) -> bool:
    """Return whether *rel_path* names a pasted copy rather than a real file.

    *rel_path* is a repository-relative path as ``git ls-tree`` reports
    it. The function is pure: same input, same answer, no filesystem and
    no subprocess — which is what lets the samples below be checked
    without a checkout.

    It is the boolean form of :func:`paste_artifact_path_rule`, which
    says *which* name rule fired. The rules themselves are written down
    once, there.
    """
    return paste_artifact_path_rule(rel_path) is not None


def _has_pasteable_extension(path: str) -> bool:
    """Return whether *path* names a file that could hold a pasted body.

    A body rule that ran over every tracked file would read binaries and
    lock files as well; the extensions are the ones the repository
    actually writes prose or code into, which is where a pasted copy of
    something lands.
    """
    return PurePosixPath(path).suffix.lstrip(".").lower() in PASTEABLE_EXTENSIONS


def paste_artifact_content_finding(text: str) -> tuple[str, int] | None:
    """Return the ``(rule, line number)`` that flags *text*, or ``None``.

    The question is about the **first non-empty line** of the body. A
    pasted diff fragment is the hunk alone: it opens with the header
    line of a hunk, ``@@ -a,b +c,d @@``. The header of a *whole* patch
    — ``diff --git``, ``--- a/...``, ``+++ b/...`` — is not that, so a
    body that opens with one of those is a patch rather than a
    fragment, and the function says so even when a hunk follows further
    down.

    A body that opens with anything else is not a fragment, and neither
    is one that is a single line that is not a hunk header. The
    function is pure: same text, same answer, no filesystem and no
    subprocess — which is what lets the samples be checked without a
    checkout.

    The line number is the line the fragment opens on, so a report can
    point at it.
    """
    if "\x00" in text:
        # A binary blob is not a pasted diff. Decoded, its bytes spell
        # anything, and "anything" is not a rule.
        return None
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if HUNK_HEADER_RE.match(line):
            return RULE_BODY_DIFF_FRAGMENT, lineno
        return None
    return None


def is_paste_artifact_content(text: str) -> bool:
    """Return whether *text* is a pasted unified-diff fragment.

    The boolean form of :func:`paste_artifact_content_finding`, which
    also says which line the fragment opens on. The rule itself is
    written down once, there.
    """
    return paste_artifact_content_finding(text) is not None


def tracked_paths_at_head(ref: str = "HEAD") -> list[str]:
    """Return every path tracked at *ref*, repository-relative.

    *ref* names the tree to read — a commit, a tag, ``HEAD``. The
    default is the tree the gate itself runs over; a historical commit
    is read exactly the same way, so the rules can be checked against
    the tree a pasted copy was really committed in.

    ``-z`` keeps the output NUL-separated so a name holding a space, a
    parenthesis or a non-ASCII character survives the round trip
    unmangled — which matters here, because the names this gate is about
    are exactly the ones a line-splitting reader would break.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-tree", "-r", "-z", "--name-only", ref],
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [path for path in raw.split("\0") if path]


def _paste_artifact_scan(
    paths: list[str],
    contents: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> tuple[list[str], list[str], dict[str, str]]:
    """Return the (name hits, body hits, bodies) among *paths*.

    The bodies come back with the hits because a report that says *which*
    rule fired on *which line* has to have read the body the body rule
    read; the two-argument form below is the answer the gate has always
    given, and it is the one that calls this.
    """
    name_hits = sorted(path for path in paths if is_paste_artifact_path(path))
    flagged = set(name_hits)
    candidates = [
        path
        for path in paths
        if path in flagged or _has_pasteable_extension(path)
    ]
    if not candidates:
        return name_hits, [], {}
    bodies = blob_contents_at_head(candidates, ref=ref) if contents is None else contents
    body_hits = sorted(
        path for path in candidates if is_paste_artifact_content(bodies.get(path, ""))
    )
    return name_hits, body_hits, bodies


def paste_artifact_scan(
    paths: list[str],
    *,
    contents: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> tuple[list[str], list[str]]:
    """Return the (name hits, body hits) among *paths*, both sorted.

    The two halves are returned separately rather than as one list
    because they answer two different questions, and a report that only
    named their union could not say *which* rule fired. The name rule
    reads the name; the body rule reads the body.

    The body rule is asked of two kinds of path: every path whose
    extension could hold a pasted body, and every path the name rule
    already flagged. The second is not redundant. A copy whose name says
    "here is the complete diff" is precisely the file whose body *is* the
    diff, and its name ends in a note rather than in an extension — so
    the extension filter on its own would never read it, and the copy
    would be named but never examined.

    *contents* supplies the bodies of *paths*. When it is ``None`` the
    bodies are read from *ref* — the same tree the names come from —
    so the two halves of the rule read one tree.

    ``blob_contents_at_head`` is defined further down this module; the
    name is resolved when this function runs, not when it is defined.
    """
    name_hits, body_hits, _ = _paste_artifact_scan(paths, contents=contents, ref=ref)
    return name_hits, body_hits


@dataclass(frozen=True)
class PasteArtifactFinding:
    """One pasted copy: where it is, which line, and which rule flagged it.

    A file can carry two of these — a name that says so *and* a body
    that is a diff fragment — because those are two rules, and a report
    that merged them into one line would not say what was wrong with
    the file.

    The line number of a name finding is ``1``: the name rule reads the
    path as a whole, and the path is not a line of anything.
    """

    path: str
    lineno: int
    rule: str

    def format(self) -> str:
        """Return the one line a report prints for this finding."""
        return f"[{self.rule}]  {self.path}:{self.lineno}: {RULE_REASONS[self.rule]}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PasteArtifactFinding({self.format()})"


def paste_artifact_findings(
    paths: list[str],
    contents: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> list[PasteArtifactFinding]:
    """Return one finding per rule that fired among *paths*.

    The findings are sorted by path, then line, then rule, so that two
    runs over the same tree print the same report in the same order.

    *contents* and *ref* mean what they mean in :func:`paste_artifact_scan`.
    """
    name_hits, body_hits, bodies = _paste_artifact_scan(
        paths, contents=contents, ref=ref
    )
    findings: list[PasteArtifactFinding] = []
    for path in name_hits:
        rule = paste_artifact_path_rule(path)
        if rule is not None:
            findings.append(PasteArtifactFinding(path=path, lineno=1, rule=rule))
    for path in body_hits:
        finding = paste_artifact_content_finding(bodies.get(path, ""))
        if finding is None:
            continue
        rule, lineno = finding
        findings.append(PasteArtifactFinding(path=path, lineno=lineno, rule=rule))
    return sorted(findings, key=lambda f: (f.path, f.lineno, f.rule))


def paste_artifact_paths(
    paths: list[str],
    contents: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> list[str]:
    """Return the pasted copies among *paths*, sorted and de-duplicated.

    A path is a pasted copy when its **name** says so, or when its
    **body** is a pasted diff fragment. The name rule catches the copy
    whose name still carries the note it was pasted with; the body rule
    catches the copy whose name was cleaned up before it was committed,
    which is the case no name rule can see.

    *contents* supplies the bodies of *paths*. When it is ``None`` the
    bodies are read from *ref* — the same tree the names come from —
    so the two halves of the rule read one tree. The default, ``HEAD``,
    is the tree the gate runs over; a historical commit is read the same
    way, which is what lets the rules be checked against the copies that
    were really committed.
    """
    name_hits, body_hits = paste_artifact_scan(paths, contents=contents, ref=ref)
    return sorted(set(name_hits) | set(body_hits))


def paste_artifact_report(offenders: list[str]) -> str:
    """Return the report naming every pasted copy, one per line.

    Every offender is named: a reader has to be able to delete the
    files without a second pass over the tree, and a report that named
    only the first offender would let the second one stay.
    """
    return "\n".join(f"  {path}" for path in offenders)


def _is_router_receiver(node: ast.expr) -> bool:
    """Return whether *node* names something a route can be declared on."""
    if not isinstance(node, ast.Name):
        return False
    name = node.id
    return name == "app" or name.endswith("router") or name.endswith("_app")


def module_declares_verification_route(source: str) -> bool:
    """Return whether *source* declares a route under the verification prefix.

    The question is about the *declaration*, not about a mention: a
    docstring that names the endpoint, an HTTP client call that reads it
    and a route that sits next to it all mention the prefix, and none of
    them implements the API.

    A module that builds its path at runtime — a constant folded into a
    concatenation, or a router carrying its own prefix — is invisible to
    this function. That limitation is stated here rather than papered
    over by a rule that would also match prose.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ROUTE_METHOD_NAMES:
            continue
        if not _is_router_receiver(func.value):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if first.value.startswith(VERIFICATION_ROUTE_PREFIX):
            return True
    return False


def _is_test_path(path: str) -> bool:
    """Return whether *path* belongs to a test tree rather than to the app."""
    pure = PurePosixPath(path)
    if any(part in {"tests", "test"} for part in pure.parts):
        return True
    return pure.name.startswith("test_")


def _git_grep_paths(needle: str, pathspec: str, ref: str = "HEAD") -> set[str]:
    """Return the tracked paths under *pathspec* whose text holds *needle*.

    ``git grep`` reads the committed tree rather than the working tree,
    so the answer does not depend on what happens to be checked out.
    *ref* names the tree to read, and ``-z`` keeps the answer
    NUL-separated, so a path holding a non-ASCII character arrives
    unmangled rather than in the quoted, octal-escaped form a
    line-splitting reader would hand over.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "grep",
            "-l",
            "-z",
            "-F",
            "-e",
            needle,
            ref,
            "--",
            pathspec,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            "git grep failed while scanning the tracked tree: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )
    out = result.stdout.decode("utf-8", errors="surrogateescape")
    prefix = f"{ref}:"
    found: set[str] = set()
    for entry in out.split("\0"):
        if not entry.startswith(prefix):
            continue
        found.add(entry[len(prefix) :])
    return found


def blob_contents_at_head(paths: list[str], ref: str = "HEAD") -> dict[str, str]:
    """Return the committed text of *paths* at *ref*, read in one batch.

    The answer of ``git cat-file --batch`` carries the size of every
    blob, so the bodies are read by size rather than by splitting the
    answer on lines: a body that holds a line shaped like a header
    cannot shift the reading of the next blob.

    The request protocol is one object name per line, and there is no
    NUL-separated variant of it — so a path holding a newline cannot be
    requested this way. The tracked set of this repository holds none.
    """
    if not paths:
        return {}
    request = "".join(f"{ref}:{path}\n" for path in paths)
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "--batch"],
        input=request.encode("utf-8", errors="surrogateescape"),
        check=True,
        capture_output=True,
    )
    out = result.stdout
    contents: dict[str, str] = {}
    pos = 0
    for path in paths:
        end = out.find(b"\n", pos)
        if end == -1:
            break
        header = out[pos:end].decode("utf-8", errors="replace")
        fields = header.split()
        pos = end + 1
        if len(fields) < 3 or fields[1] != "blob":
            continue
        size = int(fields[2])
        contents[path] = out[pos : pos + size].decode("utf-8", errors="replace")
        pos += size + 1  # cat-file --batch separates a blob from the next header
    return contents


def blob_at_revision(revision: str, path: str) -> str | None:
    """Return the text of *path* at *revision*, or ``None`` if it is not there.

    Used to read back a body that was really committed, so a rule can be
    checked against the artefact itself rather than against a sample of
    it. The revision is a commit, so the answer does not depend on what
    happens to be checked out.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{revision}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def staged_paths() -> list[str]:
    """Return the paths staged for the next commit, repository-relative.

    The index, not the working tree: a file can be edited back to clean
    after it was staged, and the commit still carries what was staged.

    ``--diff-filter=ACMR`` is added, copied, modified and renamed — the
    four ways a path can come to hold something it did not hold in
    ``HEAD``. Deleted paths are left out: there is nothing left to read.

    ``-z`` keeps the names NUL-separated, for the same reason the
    tracked-set reader keeps them NUL-separated: the names this gate is
    about are exactly the ones a line-splitting reader would mangle.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMR",
        ],
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [path for path in raw.split("\0") if path]


def blob_contents_at_index(paths: list[str]) -> dict[str, str]:
    """Return the staged text of *paths*, read from the index in one batch.

    Git addresses the index with a ``:path`` object name, and the batch
    reader above builds its request as ``<ref>:<path>`` — so an **empty**
    ref is the index. This function is the only place in this module
    that reads the index, and the only place that uses an empty ref.

    The bodies are the ones the commit will carry, not the ones on disk.
    That is the whole point of reading the index rather than the working
    tree.
    """
    return blob_contents_at_head(paths, ref="")


def verification_route_modules(
    paths: list[str],
    *,
    contents: dict[str, str] | None = None,
    ref: str = "HEAD",
) -> list[str]:
    """Return the tracked modules that declare a verification run API route.

    The scan is narrowed twice: first by path — Python modules under
    ``backend/`` that are not part of a test tree — and then by text,
    because only a module that mentions the prefix at all can possibly
    declare a route under it. The declaration itself is decided by
    :func:`module_declares_verification_route`.

    *contents* supplies the bodies of *paths*, so the scan can be run
    over a tree that is not this repository's: the candidates come from
    *paths* and the bodies come from *contents*, and the two halves read
    one tree — exactly as they do when the bodies are read from the
    commit. A path with no body in *contents* is read as an empty body,
    and an empty body declares nothing.

    When *contents* is ``None`` — the case the gate itself runs in — the
    bodies are read from *ref*, so the answer depends on the commit
    rather than on what happens to be checked out.
    """
    candidates = {
        path
        for path in paths
        if path.startswith("backend/")
        and path.endswith(".py")
        and not _is_test_path(path)
    }
    if not candidates:
        return []
    if contents is None:
        mentioned = _git_grep_paths(VERIFICATION_ROUTE_PREFIX, "backend", ref=ref)
        shortlist = sorted(candidates & mentioned)
        if not shortlist:
            return []
        bodies = blob_contents_at_head(shortlist, ref=ref)
    else:
        bodies = {path: contents.get(path, "") for path in candidates}
        shortlist = sorted(
            path for path in candidates if VERIFICATION_ROUTE_PREFIX in bodies[path]
        )
    return [
        path
        for path in shortlist
        if module_declares_verification_route(bodies.get(path, ""))
    ]


def verification_route_modules_report(modules: list[str]) -> str:
    """Return the report naming every module that serves the verification API.

    Every offender is named, one per line: a reader has to be able to
    delete the files without a second pass over the tree, and a report
    that named only the first offender would let the second one stay.
    """
    report = [
        f"the verification run API must be served by exactly one module, "
        f"{VERIFICATION_ROUTES_MODULE!r}, but the scan says {modules!r}. "
        "A second implementation is a copy, whether or not its name says so."
    ]
    if len(modules) > 1:
        report.append("every one of these serves that API:")
        report.extend(f"  {path}" for path in modules)
    return "\n".join(report)


#: The two pasted copies that were committed to this repository. They
#: are kept as samples rather than as a blacklist: the rule is written
#: over shapes, and these two names are the proof that the shapes match
#: something that was really committed.
HISTORICAL_PASTE_ARTIFACTS: tuple[str, ...] = (
    "backend/tests/static_gates/test_frozen_hash_refresh_contract.py"
    " （新建，135 行，全文见下）",
    "backend/tests/static_gates/test_verification_mode_scope_gates.py"
    " （修改，1070 行 —— 仅两张冻结哈希表内的哈希值与注释变动，"
    "其余一字未动；下面给出完整 diff，不重贴全文以免写回时截断）",
)

#: The two real gate modules whose names the copies borrowed. A file
#: that shares a module's name is only a copy because of the *rest* of
#: its name, so these two paths are the negative control for the rule:
#: the same function, the same shape of argument, the opposite answer.
#:
#: The third entry is the module the *earlier* pair of copies borrowed
#: their name from — ``backend/routes/verification.py`` — which is the
#: module VP-025 names as the only implementation of ``routes.verification``.
REAL_MODULE_PATHS: tuple[str, ...] = (
    "backend/tests/static_gates/test_verification_mode_scope_gates.py",
    "backend/tests/static_gates/test_frozen_hash_refresh_contract.py",
    "backend/routes/verification.py",
)

#: A copy whose name *describes* it rather than naming it: no marker
#: word, just the extension followed by a parenthesis. It carries the
#: structural rule alone, which is what keeps that rule load-bearing.
STRUCTURAL_ONLY_SAMPLE: str = (
    "backend/tests/static_gates/test_verification_mode_scope_gates.py （副本）"
)

#: A copy outside the source roots, carrying a marker word alone. It
#: keeps the marker rule load-bearing on its own, and it keeps that rule
#: honest about its scope: a copy is a copy wherever it sits.
MARKER_ONLY_SAMPLE: str = "docs/notes（modified）.md"

#: Ordinary tracked paths. None of them may be flagged — the list is
#: the positive control that keeps the two rules from growing into "any
#: name with a parenthesis in it".
ORDINARY_PATHS: tuple[str, ...] = (
    "backend/routes/verification.py",
    "backend/state_machine/db/schema.py",
    "backend/tests/static_gates/test_verification_mode_scope_gates.py",
    "frontend/app.js",
    "frontend/index.html",
    "frontend/style.css",
    "scripts/run_tests.sh",
    "tools/pdt-secret-reader/setup.sh",
    "docs/development/cross-repo-contract.md",
)

#: A body that opens with the header line of a unified-diff hunk. This
#: is the shape a pasted diff fragment has, and it is the shape that
#: survives a cleanup of the *name*: the name can be made ordinary, but
#: the body still opens with ``@@``.
DIFF_FRAGMENT_SAMPLE: str = (
    "@@ -1,2 +1,3 @@\n"
    " unchanged line\n"
    "+added line\n"
)

#: The real bodies of the two historical copies whose content is a
#: unified diff, copied verbatim from the blobs committed in
#: ``e8b568f4``. They are samples rather than a blacklist: what they pin
#: is the *shape* of a pasted diff fragment, so the rule is checked
#: against bodies that were really committed.
HISTORICAL_DIFF_BODY_SAMPLES: tuple[str, ...] = (
    "@@ -190,6 +190,14 @@ from plan_scheduler.snapshot import build\n"
    " import server as _server\n"
    "+\n",
    "@@ -654,11 +654,16 @@ FROZEN_R10_SURFACE_SHA256: dict[str, str] = {\n"
    "     # 重新钉住该文件的哈希。\n"
    "+    # 2026-11-01：因「调度卡片按卡片渲染、默认收起、"
    "展开才请求详情」（task-12）改动了\n",
)

#: The third historical copy is a *full text*, not a diff: its body
#: opens with a docstring. It is the negative control for the content
#: rule — a pasted copy whose body a content rule cannot see, which is
#: exactly why the name rule still has to exist beside it.
FULL_TEXT_PASTE_SAMPLE: str = (
    '"""The frozen-hash refresh contract for the three changed frontend files.\n'
    "\n"
    "Why this gate exists\n"
    "--------------------\n"
)

#: Bodies that open with the header of a *whole* patch. A patch file
#: carries a header; a pasted fragment is the hunk alone. These samples
#: deliberately carry a hunk header further down, so the rule cannot be
#: "does the body contain a hunk" — it is "does the body *open* with
#: one".
DIFF_HEADER_SAMPLES: tuple[str, ...] = (
    "diff --git a/backend/routes/verification.py b/backend/routes/verification.py\n"
    "index 1234567..89abcde 100644\n"
    "@@ -1,2 +1,3 @@\n",
    "--- a/backend/routes/verification.py\n"
    "+++ b/backend/routes/verification.py\n"
    "@@ -1,2 +1,3 @@\n",
    "+++ b/backend/routes/verification.py\n"
    "@@ -1,2 +1,3 @@\n",
)

#: Ordinary bodies: real source, real markdown, real configuration.
#: None of them opens with a hunk header, and the rule must not mistake
#: a file that merely *mentions* a diff for one.
ORDINARY_BODY_SAMPLES: tuple[str, ...] = (
    '"""A module docstring."""\n\n\ndef helper() -> int:\n    return 1\n',
    "# Title\n\nSome prose.\n\n```diff\n@@ -1,2 +1,3 @@\n```\n",
    "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n",
)

#: The commit that introduced the pasted copies. It is an ancestor of
#: the base branch, which is why a cleanup that deletes the copies by
#: name has to be repeated after every merge — and why the bodies can be
#: read back to check the content rule against them.
PASTE_ARTIFACT_COMMIT: str = "e8b568f4"

#: The three historical copies, with the shape of their bodies: two are
#: diff fragments, one is a full text.
HISTORICAL_COPY_BODIES: tuple[tuple[str, bool], ...] = (
    (
        "backend/routes/scheduler.py *(modified — full diff; the file is "
        "818 lines, unchanged parts omitted)*",
        True,
    ),
    (
        "backend/tests/static_gates/test_verification_mode_scope_gates.py"
        " （修改，1070 行 —— 仅两张冻结哈希表内的哈希值与注释变动，"
        "其余一字未动；下面给出完整 diff，不重贴全文以免写回时截断）",
        True,
    ),
    (
        "backend/tests/static_gates/test_frozen_hash_refresh_contract.py"
        " （新建，135 行，全文见下）",
        False,
    ),
)

#: The three historical copies, by the path they were committed under.
#: Derived from the bodies above rather than written out a second time,
#: so the two lists cannot drift apart.
HISTORICAL_PASTE_ARTIFACT_PATHS: tuple[str, ...] = tuple(
    path for path, _is_diff in HISTORICAL_COPY_BODIES
)

#: The two of them whose body is a diff fragment — and therefore the two
#: the body rule has to find, while the third is a full text that the
#: body rule cannot see.
HISTORICAL_DIFF_ARTIFACT_PATHS: tuple[str, ...] = tuple(
    path for path, is_diff in HISTORICAL_COPY_BODIES if is_diff
)

#: The number of paths tracked at ``e8b568f4``. It is pinned so the
#: historical scan cannot quietly read a different tree: the current one
#: holds more paths than this, and a scan that read it would answer with
#: a different set — and with no copy at all.
HISTORICAL_TRACKED_PATH_COUNT: int = 1941


def test_the_repository_reports_its_tracked_paths() -> None:
    """The derivation must produce something, or the rule below is vacuous.

    A broken invocation, a changed output format, or a run outside a
    checkout would each leave the tracked set empty — and an empty set
    satisfies "no tracked path is a pasted copy" without checking
    anything.
    """
    tracked = tracked_paths_at_head()
    assert tracked, (
        "git ls-tree reported no tracked paths under HEAD — the derivation "
        "is broken, and the assertion below would pass over an empty set."
    )


def test_no_tracked_path_is_a_paste_artifact() -> None:
    """No tracked path may carry the name of a pasted copy.

    Every offender is printed, one per line, because the list is the
    whole of the report: a reader has to be able to delete the files
    without a second pass over the tree.
    """
    offenders = paste_artifact_paths(tracked_paths_at_head())
    for path in offenders:
        print(f"pasted copy in the tracked set: {path}")
    assert not offenders, (
        "these tracked paths are pasted copies — a file whose name was "
        "taken from a message and written into the tree verbatim, or a "
        "file whose body is a pasted diff fragment. They are not "
        "imported by anything, they mislead every later import, static "
        "gate and audit that walks the tree, and a copy deleted by name "
        "returns the moment the commit that introduced it is merged back "
        "in. Delete them:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_flags_the_known_artifact_shapes() -> None:
    """The two committed copies are flagged; the modules they copied are not.

    Both directions are asserted here, on one sample set, because a rule
    that only ever answers True satisfies the first half on its own and
    a rule that only ever answers False satisfies the second. Together
    they are the negative control the gate needs: the same function, the
    same shape of argument, two opposite answers.

    The two flagged names are the paths that were really committed, kept
    as samples so the rule cannot drift into a shape nobody has ever
    met. The two unflagged names are the real gate modules the copies
    were pasted from — the same directory, the same extensions, the same
    prefixes, and not a copy.
    """
    for path in HISTORICAL_PASTE_ARTIFACTS:
        assert is_paste_artifact_path(path), (
            f"{path!r} is a pasted copy that was committed to this "
            "repository, and the rule no longer recognises it."
        )
        assert STRUCTURAL_PASTE_RE.search(PurePosixPath(path).name), (
            f"{path!r} was committed with a source extension followed by a "
            "parenthesis, so the structural rule has to recognise it too. "
            "A marker word is a note a person wrote once; the shape is "
            "what the next copy will have, and it has to be caught by the "
            "rule that generalises."
        )
    for path in REAL_MODULE_PATHS:
        assert not is_paste_artifact_path(path), (
            f"{path!r} is a real gate module, not a pasted copy, and the "
            "rule must not flag it. A rule that flags the repository's "
            "own modules has stopped being a rule."
        )


def test_the_structural_rule_fires_on_its_own() -> None:
    """A copy named rather than described is still a copy.

    The sample carries no marker word, so only the structural rule can
    catch it. Without this test the structural rule could be dropped
    and the gate would still be green on the two historical names.
    """
    assert not MARKER_RE.search(PurePosixPath(STRUCTURAL_ONLY_SAMPLE).name)
    assert is_paste_artifact_path(STRUCTURAL_ONLY_SAMPLE), (
        "a source path whose extension is followed by a parenthesis is a "
        "named copy, and the structural rule has to say so on its own."
    )


def test_the_marker_rule_fires_on_its_own() -> None:
    """A copy described by a note is still a copy, wherever it sits.

    The sample is outside the source roots and carries no source
    extension, so only the marker rule can catch it — and it keeps the
    marker rule from being quietly narrowed to the source roots.
    """
    assert not STRUCTURAL_PASTE_RE.search(PurePosixPath(MARKER_ONLY_SAMPLE).name)
    assert is_paste_artifact_path(MARKER_ONLY_SAMPLE), (
        "a name carrying a note that only a pasted copy would carry is a "
        "copy, and the marker rule has to say so on its own."
    )


def test_ordinary_paths_are_not_flagged() -> None:
    """The rules must not fire on the files the repository actually has.

    A rule that catches everything is a rule that has stopped being a
    rule: it fails on the first legitimate file anybody adds, and the
    fix — loosening the rule — is what lets the next copy through.
    """
    flagged = [path for path in ORDINARY_PATHS if is_paste_artifact_path(path)]
    assert not flagged, (
        "these ordinary tracked paths are flagged as pasted copies, so "
        "the rule has grown into something that fires on the repository "
        "itself:\n  " + "\n  ".join(flagged)
    )


def test_the_rule_is_neither_always_true_nor_always_false() -> None:
    """The rule must answer both ways on the same sample set.

    This is the property that makes the negative controls above
    meaningful: a function that returned one constant answer would
    satisfy either control alone.
    """
    samples = list(HISTORICAL_PASTE_ARTIFACTS) + [
        STRUCTURAL_ONLY_SAMPLE,
        MARKER_ONLY_SAMPLE,
        *ORDINARY_PATHS,
    ]
    verdicts = {is_paste_artifact_path(path) for path in samples}
    assert verdicts == {True, False}, (
        "the rule must answer both True and False over the sample set, "
        f"but it answered {sorted(verdicts)} — a rule that answers one "
        "way only is not a rule."
    )


def test_the_scan_is_idempotent_and_reads_nothing_written() -> None:
    """Two scans of the same tree agree, and the tree is unchanged.

    The gate has to be safe to run on every commit: same answer twice,
    no write of any kind. The head commit is compared before and after,
    so a scan that staged, committed or moved anything would be caught.
    """
    before = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    first = paste_artifact_paths(tracked_paths_at_head())
    second = paste_artifact_paths(tracked_paths_at_head())
    after = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert first == second, (
        "two scans of the same tree disagreed — the scan is not "
        f"idempotent: {first!r} then {second!r}"
    )
    assert before == after, (
        f"the scan moved HEAD from {before} to {after}; the gate is "
        "supposed to read the tree and write nothing."
    )


def test_the_modules_the_deleted_copies_borrowed_their_names_from_are_still_tracked() -> None:
    """A pasted copy may be deleted. The module it copied may not.

    The copies that were removed from this repository were removed
    *because* the real modules beside them were there — a copy is only
    deletable while its source survives. Nothing above checks that the
    source survives: a later commit that deleted the real module would
    leave this gate green, and the repository would quietly have lost a
    gate module instead of a copy.

    So the sources are pinned here, in the same tracked set the name
    rules read.
    """
    tracked = set(tracked_paths_at_head())
    missing = [path for path in REAL_MODULE_PATHS if path not in tracked]
    assert not missing, (
        "these modules are the sources the deleted copies were pasted "
        "from, and they are no longer tracked:\n  " + "\n  ".join(missing)
        + "\nA deleted copy is a cleanup; a deleted source is a loss."
    )


def test_the_verification_routes_module_is_the_only_implementation() -> None:
    """No second module may serve the verification run API.

    VP-025's expected_result names ``backend/routes/verification.py`` as
    the *only* implementation of ``routes.verification``. The name rules
    above cannot enforce that on their own: a copy whose name was cleaned
    up before it was committed — ``verification_v2.py``,
    ``verification_helper.py`` — carries no marker word and no
    parenthesis, so it walks past every rule that reads names.

    What does not walk past is the *surface*. The verification run API is
    served under one prefix, and a module that serves it is an
    implementation of it. So this test reads the tracked modules and asks
    which ones declare a route under that prefix.

    The neighbour that must not be counted is ``backend/routes/phases.py``:
    it serves ``/api/verification-plan/...`` — the *plan* of a verification
    phase, a different surface — and a rule that matched it would be
    matching on the word "verification" rather than on the API.
    """
    modules = verification_route_modules(tracked_paths_at_head())
    assert modules == [VERIFICATION_ROUTES_MODULE], verification_route_modules_report(
        modules
    )


def test_the_route_detector_separates_the_verification_surface_from_its_neighbours() -> None:
    """The detector answers on the prefix, not on the word "verification".

    Each case is a line of Python and carries the expected answer. The
    negative cases are what make the rule a rule: the neighbouring surface
    (``/api/verification-plan/``), an unrelated route, an HTTP client
    call that is not a route at all, and a mention of the endpoint in a
    docstring — none of which declares the verification run API.
    """
    cases = (
        ('router.get("/api/verification/{plan_id}/status")', True),
        ('router.post("/api/verification/{plan_id}/start")', True),
        ('router.get("/api/verification-plan/{plan_id}")', False),
        ('router.get("/api/status")', False),
        ('requests.get("/api/verification/{plan_id}/status")', False),
        ('"""POST /api/verification/{id}/start starts the loop."""', False),
    )
    for source, expected in cases:
        assert module_declares_verification_route(source) is expected, (
            f"{source!r} should be read as "
            f"{'declaring' if expected else 'not declaring'} the "
            "verification run API, and the detector said otherwise."
        )


def test_the_content_rule_fires_on_a_unified_diff_fragment() -> None:
    """A body that opens with a diff hunk header is a pasted fragment.

    The name rule cannot see this copy: the name was cleaned up before
    the file was committed, so the *body* is the only thing left that
    says what the file is.
    """
    assert is_paste_artifact_content(DIFF_FRAGMENT_SAMPLE), (
        "a body whose first non-empty line is a unified-diff hunk header "
        "is a pasted diff fragment, and the content rule has to say so."
    )
    assert is_paste_artifact_content("\n\n" + DIFF_FRAGMENT_SAMPLE), (
        "blank lines above the hunk header do not change what the first "
        "non-empty line is."
    )


def test_the_historical_diff_bodies_are_flagged_by_content() -> None:
    """The two historical copies whose body is a diff are flagged."""
    for body in HISTORICAL_DIFF_BODY_SAMPLES:
        assert is_paste_artifact_content(body), (
            "the body of a copy that was really committed to this "
            "repository opens with a diff hunk header, and the content "
            "rule no longer recognises it."
        )


def test_the_historical_bodies_in_git_history_are_flagged_by_content() -> None:
    """The real blobs committed in ``e8b568f4`` are flagged, not only samples.

    The samples above are copies of these bodies; this test reads the
    blobs themselves, so the rule is checked against what was really
    committed rather than against what a sample happens to say.
    """
    for path, is_diff in HISTORICAL_COPY_BODIES:
        body = blob_at_revision(PASTE_ARTIFACT_COMMIT, path)
        if body is None:
            pytest.skip(
                f"{PASTE_ARTIFACT_COMMIT} is not in this checkout, so the "
                "historical bodies cannot be read back"
            )
        assert is_paste_artifact_content(body) is is_diff, (
            f"the body of {path!r} at {PASTE_ARTIFACT_COMMIT} "
            f"{'opens' if is_diff else 'does not open'} with a diff hunk "
            "header, and the content rule said otherwise."
        )


def test_the_content_rule_ignores_the_header_of_a_whole_patch() -> None:
    """A body that opens with a patch header is a patch, not a fragment.

    ``diff --git``, ``--- a/...`` and ``+++ b/...`` are the header of a
    patch file. A pasted fragment is the hunk alone, so a body that
    opens with one of those is not a fragment — and the samples carry a
    hunk header further down, so a rule that merely searched the body
    would wrongly claim them.
    """
    for body in DIFF_HEADER_SAMPLES:
        assert not is_paste_artifact_content(body), (
            "a body that opens with the header of a whole patch is a "
            "patch, not a pasted fragment, and the content rule must not "
            "claim it."
        )


def test_the_content_rule_ignores_a_full_text_paste() -> None:
    """A pasted full text opens with prose, not with a hunk header.

    This is the blind spot of the content rule, and it is stated here
    rather than hidden: a copy of a whole file is invisible to it. The
    name rule is what catches that one, which is why both rules exist.
    """
    assert not is_paste_artifact_content(FULL_TEXT_PASTE_SAMPLE), (
        "a body that opens with a docstring is a pasted full text, not a "
        "diff fragment, and the content rule must not claim it."
    )


def test_ordinary_bodies_are_not_flagged_by_content() -> None:
    """Ordinary source, markdown and configuration are not diff fragments."""
    for body in ORDINARY_BODY_SAMPLES:
        assert not is_paste_artifact_content(body), (
            "an ordinary body is not a pasted diff fragment, and the "
            "content rule must not claim it."
        )
    contents = blob_contents_at_head(list(ORDINARY_PATHS))
    flagged = sorted(
        path for path, body in contents.items() if is_paste_artifact_content(body)
    )
    assert not flagged, (
        "the bodies of these ordinary tracked files are flagged as pasted "
        "diff fragments, so the content rule has grown into something "
        "that fires on the repository itself:\n  " + "\n  ".join(flagged)
    )


def test_a_copy_whose_name_was_cleaned_up_is_caught_by_its_body() -> None:
    """The name rule cannot see it; the content rule has to."""
    path = "backend/routes/verification_helper.py"
    assert not is_paste_artifact_path(path), (
        "the sample is a name with no marker word and no parenthesis, so "
        "the name rule must not flag it — otherwise this test would not "
        "be testing the content rule at all."
    )
    offenders = paste_artifact_paths([path], contents={path: DIFF_FRAGMENT_SAMPLE})
    assert path in offenders, (
        "a tracked file whose name is ordinary but whose body is a pasted "
        "diff fragment is a pasted copy, and the scan has to report it."
    )


# ---------------------------------------------------------------------------
# The enumeration half, run over trees that are not this repository's
# ---------------------------------------------------------------------------
#
# The gate above proves that *today* there is one implementation of the
# verification run API. It does not prove that the scan would *notice* a
# second one: a scan that always answered with the one module it knows
# would satisfy it just as well. So the scan is exercised here over
# synthetic trees — trees that are never committed, and that never touch
# this repository — in which the second implementation exists.


def _working_tree_state() -> str:
    """Return a fingerprint of the working tree, to prove a test wrote nothing."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


#: A body that declares a route under the verification run API. This is
#: what a second implementation looks like: the same surface, served by a
#: module whose name says nothing about it.
SYNTHETIC_VERIFICATION_ROUTE_BODY: str = (
    "from fastapi import APIRouter\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.post("/api/verification/{plan_id}/start")\n'
    "def start(plan_id: str) -> dict:\n"
    '    return {"plan_id": plan_id}\n'
)

#: A body that serves the *plan* of a verification phase — a different
#: surface, under a different prefix.
SYNTHETIC_VERIFICATION_PLAN_BODY: str = (
    "from fastapi import APIRouter\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.get("/api/verification-plan/{plan_id}")\n'
    "def plan(plan_id: str) -> dict:\n"
    '    return {"plan_id": plan_id}\n'
)

#: A body that serves neither surface. It is here so that the "nothing
#: declares the prefix" case has modules to be: the scan has to answer
#: "no module", not "no input".
SYNTHETIC_ORDINARY_BODY: str = (
    "from fastapi import APIRouter\n"
    "\n"
    "router = APIRouter()\n"
    "\n"
    "\n"
    '@router.get("/api/status")\n'
    "def status() -> dict:\n"
    '    return {"ok": True}\n'
)


def test_a_second_module_serving_the_verification_api_is_reported() -> None:
    """The scan must report a second implementation, and name both.

    The gate over the real tree proves there is one implementation today.
    It does not prove the scan would *notice* a second one, so the scan
    is run here over a synthetic tree in which two modules declare the
    verification run API. Both have to be reported: a scan that names
    only one of them leaves the other in the tree.
    """
    modules = {
        "backend/routes/verification.py": SYNTHETIC_VERIFICATION_ROUTE_BODY,
        "backend/routes/verification_v2.py": SYNTHETIC_VERIFICATION_ROUTE_BODY,
    }
    found = verification_route_modules(sorted(modules), contents=modules)
    assert found == sorted(modules), (
        "two modules declare the verification run API, and the scan has to "
        f"report both of them; it reported {found!r}. A scan that reports "
        "one of them lets the other stay in the tree."
    )
    report = verification_route_modules_report(found)
    for path in sorted(modules):
        assert path in report, (
            f"the report has to name {path!r} as well — a report that names "
            "only one of the two offenders lets the other one stay."
        )


def _assert_the_neighbouring_surface_is_not_counted(
    modules: dict[str, str],
) -> list[str]:
    """Assert that the ``-plan`` neighbour is not counted as an offender.

    Factored into a helper so the negative control below can run *this*
    assertion with a widened prefix and watch it go red. That is what
    shows the assertion is load-bearing rather than vacuously true.
    """
    found = verification_route_modules(sorted(modules), contents=modules)
    assert found == [VERIFICATION_ROUTES_MODULE], (
        f"only {VERIFICATION_ROUTES_MODULE!r} serves the verification run "
        f"API, but the scan reported {found!r}. A module serving "
        "/api/verification-plan/... serves a different surface and must "
        "not be counted as a second implementation."
    )
    return found


def test_the_neighbouring_verification_plan_surface_is_not_counted() -> None:
    """``/api/verification-plan/...`` is a different surface, not a copy.

    The neighbour is the reason the prefix carries a trailing slash: the
    two surfaces differ by exactly that character.
    """
    modules = {
        "backend/routes/verification.py": SYNTHETIC_VERIFICATION_ROUTE_BODY,
        "backend/routes/verification_plan.py": SYNTHETIC_VERIFICATION_PLAN_BODY,
    }
    _assert_the_neighbouring_surface_is_not_counted(modules)


def test_the_scan_answers_nothing_when_no_module_declares_the_prefix() -> None:
    """A tree in which nothing declares the API answers with an empty list.

    The empty answer has to be an answer and not an error: the scan runs
    over every commit, and a tree that has not grown a route module yet
    is an ordinary thing to scan.
    """
    modules = {
        "backend/routes/verification.py": SYNTHETIC_ORDINARY_BODY,
        "backend/routes/phases.py": SYNTHETIC_ORDINARY_BODY,
    }
    try:
        found = verification_route_modules(sorted(modules), contents=modules)
    except Exception as exc:
        pytest.fail(
            "the scan raised on a tree in which no module declares the "
            f"verification run API: {exc!r}"
        )
    assert found == [], (
        "no module in the tree declares the verification run API, so the "
        f"scan must answer with an empty list; it answered {found!r}."
    )


def test_the_trailing_slash_of_the_prefix_is_what_keeps_the_neighbour_out() -> None:
    """Widen the prefix and the neighbour is counted: the boundary is load-bearing.

    The assertion in the test above passes on a tree the scan builds
    itself, so it is worth asking what makes it pass. The answer has to
    be the trailing slash of the prefix, and not the shape of the
    sample. So the prefix is widened here — the slash is dropped — and
    the very same assertion has to go red. If it stayed green, the
    boundary would be carried by something else, and the assertion would
    not be pinning the thing it claims to pin.

    The prefix is a module-level constant, so it is restored in a
    ``finally``: the test must not leave the rest of the suite looking
    at a widened prefix. The working tree is compared before and after
    as well — the test writes nothing, not even a file.
    """
    global VERIFICATION_ROUTE_PREFIX

    modules = {
        "backend/routes/verification.py": SYNTHETIC_VERIFICATION_ROUTE_BODY,
        "backend/routes/verification_plan.py": SYNTHETIC_VERIFICATION_PLAN_BODY,
    }
    original = VERIFICATION_ROUTE_PREFIX
    before = _working_tree_state()
    try:
        VERIFICATION_ROUTE_PREFIX = original.rstrip("/")
        with pytest.raises(AssertionError):
            _assert_the_neighbouring_surface_is_not_counted(modules)
    finally:
        VERIFICATION_ROUTE_PREFIX = original
    after = _working_tree_state()

    assert VERIFICATION_ROUTE_PREFIX == original, (
        "the widened prefix was not restored, so the rest of the suite "
        f"would run against {VERIFICATION_ROUTE_PREFIX!r}."
    )
    assert before == after, (
        "the negative control changed the working tree:\n"
        f"before:\n{before}\nafter:\n{after}"
    )


def test_no_tracked_pasteable_file_carries_a_pasted_diff_body() -> None:
    """No tracked file's body is a pasted diff fragment.

    The negative control for the content rule over the whole repository:
    every tracked file that could hold a pasted body is read and asked.
    A rule that fires on a real file fails the build on the first
    legitimate file anybody adds, and the fix — loosening the rule — is
    what lets the next copy through.
    """
    tracked = tracked_paths_at_head()
    pasteable = [path for path in tracked if _has_pasteable_extension(path)]
    contents = blob_contents_at_head(pasteable)
    offenders = sorted(
        path for path, body in contents.items() if is_paste_artifact_content(body)
    )
    assert not offenders, (
        "these tracked files open with a unified-diff hunk header, so "
        "their bodies are pasted diff fragments:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# The historical half: the same rules, read over the commit that introduced
# the copies
# ---------------------------------------------------------------------------
#
# Every test above reads the tree as it is *now*. That is the right thing
# for a gate to do — but it leaves the historical claim untested. "This
# rule catches the copies that were really committed to this repository"
# is an assertion, and a rule quietly narrowed to the current tree would
# satisfy every one of those tests while the claim rotted.
#
# So the scan is pointed at a commit. The three read functions take the
# tree to read as an argument, the default still being ``HEAD``, and the
# rules are then asked the same two questions they are asked over the
# current tree.


def test_the_historical_commit_still_holds_the_copies_it_really_held() -> None:
    """The rules must recognise the copies that were really committed.

    The scan is run over ``e8b568f4`` — the commit that introduced the
    three copies — and asked the two questions separately: which paths
    did the name rule flag, and which did the body rule flag. The
    answers are pinned because they are properties of the *rules*, not of
    the moment: three names, and two bodies.

    The tracked set is pinned as well, because it is what tells the two
    trees apart. That commit holds 1941 tracked paths and the current
    one holds more, so a scan that read the wrong tree would answer with
    a different set — and with no copy at all.
    """
    tracked = tracked_paths_at_head(PASTE_ARTIFACT_COMMIT)
    assert len(tracked) == HISTORICAL_TRACKED_PATH_COUNT, (
        f"{PASTE_ARTIFACT_COMMIT} holds {HISTORICAL_TRACKED_PATH_COUNT} "
        f"tracked paths, but the scan read {len(tracked)}. The historical "
        "scan is reading a different tree than the one it claims to read."
    )

    name_hits, body_hits = paste_artifact_scan(tracked, ref=PASTE_ARTIFACT_COMMIT)

    assert name_hits == sorted(HISTORICAL_PASTE_ARTIFACT_PATHS), (
        "the name rule must flag exactly the three paths that were "
        f"committed under {PASTE_ARTIFACT_COMMIT}; it flagged "
        f"{name_hits!r}."
    )
    assert body_hits == sorted(HISTORICAL_DIFF_ARTIFACT_PATHS), (
        "the body rule must flag exactly the two of them whose body is a "
        f"diff fragment; it flagged {body_hits!r}."
    )
    assert set(body_hits) <= set(name_hits), (
        "the two copies whose body is a diff fragment are the ones whose "
        "name also carries the note they were pasted with — so the body "
        "rule has to be asked of the name hits as well, and not only of "
        "the files whose extension could hold a body."
    )


def test_the_report_names_every_historical_artifact() -> None:
    """Every one of the three copies has to be named, not just the first.

    A report that named only one offender would let the other two stay
    in the tree, and the tree it is written about is exactly the tree the
    copies were committed in.
    """
    offenders = paste_artifact_paths(
        tracked_paths_at_head(PASTE_ARTIFACT_COMMIT), ref=PASTE_ARTIFACT_COMMIT
    )
    assert offenders == sorted(HISTORICAL_PASTE_ARTIFACT_PATHS), (
        "the three copies that were committed under "
        f"{PASTE_ARTIFACT_COMMIT} are the ones the scan has to report; it "
        f"reported {offenders!r}."
    )
    report = paste_artifact_report(offenders)
    for path in HISTORICAL_PASTE_ARTIFACT_PATHS:
        assert path in report, (
            f"the report has to name {path!r} as well — a report that "
            "names only some of the offenders lets the others stay."
        )


def test_the_head_commit_carries_no_pasted_copy_in_either_half() -> None:
    """The current tree answers zero on both halves, not only on the union.

    The gate above asserts that the union is empty. That is enough to
    fail the build, but it does not say *which* rule was asked: a scan
    that answered zero on the name half and three on the body half would
    produce the same union and the same failure. The two halves are
    therefore asked separately, so that the gate watching the current
    tree is watching both rules and not one of them.
    """
    tracked = tracked_paths_at_head()
    name_hits, body_hits = paste_artifact_scan(tracked)
    assert not name_hits, (
        "the name rule flags tracked paths in the current tree:\n"
        + paste_artifact_report(name_hits)
    )
    assert not body_hits, (
        "the body rule flags tracked paths in the current tree:\n"
        + paste_artifact_report(body_hits)
    )


def test_a_clean_synthetic_tree_carries_no_pasted_copy() -> None:
    """A tree with nothing pasted into it answers zero on both halves.

    The historical scan proves that the rules *find* what was really
    committed. It does not prove that they stay quiet on a tree that
    holds nothing of the kind — and a rule that fired on everything
    would satisfy the historical scan just as well, by flagging all
    three copies and everything else along with them.

    So the scan is run over a synthetic tree of ordinary files, whose
    names are awkward in the way real names are awkward — one holds a
    space, one holds full-width characters — and the answer has to be
    zero on both halves.
    """
    paths = [
        "backend/routes/verification.py",
        "backend/routes/scheduler.py",
        "backend/tests/static_gates/test_frozen_hash_refresh_contract.py",
        "docs/说明/scheduler 面板.md",
        "frontend/app.js",
    ]
    contents = {
        "backend/routes/verification.py": ORDINARY_BODY_SAMPLES[0],
        "backend/routes/scheduler.py": ORDINARY_BODY_SAMPLES[0],
        "backend/tests/static_gates/test_frozen_hash_refresh_contract.py": (
            ORDINARY_BODY_SAMPLES[0]
        ),
        # This body mentions a diff hunk further down, so the rule has to
        # refuse it: a body that merely mentions a diff is not one.
        "docs/说明/scheduler 面板.md": ORDINARY_BODY_SAMPLES[1],
        "frontend/app.js": ORDINARY_BODY_SAMPLES[2],
    }
    name_hits, body_hits = paste_artifact_scan(paths, contents=contents)
    assert not name_hits, (
        "the name rule flags an ordinary tree:\n" + paste_artifact_report(name_hits)
    )
    assert not body_hits, (
        "the body rule flags an ordinary tree:\n" + paste_artifact_report(body_hits)
    )


def _line_split_tracked_paths(ref: str) -> list[str]:
    """Read the tracked paths the wrong way: line-separated and quoted.

    This is the reader the gate must not use. ``git ls-tree`` without
    ``-z`` prints a path holding a non-ASCII character in quoted,
    octal-escaped form, so the name a person wrote never arrives — and a
    rule written over names would never see it.
    """
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "-c",
            "core.quotePath=true",
            "ls-tree",
            "-r",
            "--name-only",
            ref,
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="surrogateescape").splitlines()


def test_the_historical_paths_are_read_nul_safely() -> None:
    """The three names have to arrive intact — and the line reader cannot.

    Two of the three historical paths hold a full-width parenthesis and
    one holds an em dash, and two of them hold Chinese. The names are
    the whole subject of the name rule, so a reader that changed them
    would change what the rule can see.

    The second half of the test is the negative control: it reads the
    same tree the wrong way and shows that the names are not in that
    answer. Without it the first half would pass on any reader at all.

    The bodies are checked as well, because the body rule reads a path
    it has to *ask* for: a name mangled on the way into the request is a
    body no rule can see.
    """
    tracked = tracked_paths_at_head(PASTE_ARTIFACT_COMMIT)
    for path in HISTORICAL_PASTE_ARTIFACT_PATHS:
        assert path in tracked, (
            f"{path!r} is one of the paths that were committed under "
            f"{PASTE_ARTIFACT_COMMIT}, and the NUL-safe read did not "
            "return it. A name mangled on the way in is a name the rule "
            "cannot see."
        )

    bodies = blob_contents_at_head(
        list(HISTORICAL_PASTE_ARTIFACT_PATHS), ref=PASTE_ARTIFACT_COMMIT
    )
    for path in HISTORICAL_PASTE_ARTIFACT_PATHS:
        assert bodies.get(path), (
            f"the body of {path!r} was not read at {PASTE_ARTIFACT_COMMIT}. "
            "A path that cannot be asked for is a body no rule can see."
        )

    mangled = _line_split_tracked_paths(PASTE_ARTIFACT_COMMIT)
    for path in HISTORICAL_PASTE_ARTIFACT_PATHS:
        assert path not in mangled, (
            f"{path!r} was found by a line-splitting, quoted read, so the "
            "NUL-safe read is not the thing that produced the answer above."
        )


# ---------------------------------------------------------------------------
# The command line — the same rule, at the moment of the commit
# ---------------------------------------------------------------------------

#: The wrapper the pre-commit hook calls. It puts the backend package on
#: ``sys.path`` and forwards to :func:`main`; it holds no rule of its own.
_CLI_SCRIPT: Path = REPO_ROOT / "scripts" / "check_no_paste_artifacts.py"

#: The pre-commit configuration the hook is declared in.
_PRECOMMIT_CONFIG: Path = REPO_ROOT / ".pre-commit-config.yaml"

#: A file name that only a pasted copy carries. The marker is spliced
#: from fragments, as elsewhere in this repository, so that this file
#: does not put the thing it rejects into the repository that rejects it.
_PASTE_ARTIFACT_NAME: str = "copied" + ".py " + "（" + "new" + "）"

#: A tree that holds a file whose *body* is a pasted diff fragment, and
#: whose name says nothing at all. The name rule cannot see it; only the
#: body rule can.
_PASTE_DIFF_BODY: str = "@@ -1,3 +1,4 @@\n context\n-removed\n+added\n"


def _run_cli(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run the wrapper script and return the completed process."""
    return subprocess.run(
        [sys.executable, str(_CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _run_git(env: dict[str, str], *args: str) -> str:
    """Run a git command in this repository and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout


def test_the_command_line_reports_a_clean_tree() -> None:
    """The default run scans the tracked tree, and this tree is clean.

    The gate above already asserts that the tracked tree holds no pasted
    copy. This asserts the *command* says so too, in the exit code the
    pre-commit hook reads: a command that answered 1 here would block
    every commit in this repository, and a command that answered 0 while
    the tree held one would be a gate that never fires.
    """
    result = _run_cli()
    assert result.returncode == 0, (
        f"the command reported {result.returncode} on a clean tree; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "0 violation" in result.stdout, (
        f"the command did not report the count it found: {result.stdout!r}"
    )


def test_the_command_line_refuses_a_pasted_copy_in_a_temporary_tree(tmp_path) -> None:
    """Negative control: a tree holding a pasted copy is refused.

    The control has to exist, because a command that scans nothing
    answers "clean" for ever. So a tree is built under ``tmp_path``
    holding one file whose name only a pasted copy carries, and the
    command is asked about that tree.

    The rule the command reports has to be the rule *this module* names
    for that path — read off :func:`paste_artifact_path_rule` rather
    than spelled out here, so that a command carrying a copy of the
    rules cannot pass for one that calls them.
    """
    tree = tmp_path / "tree"
    (tree / "backend").mkdir(parents=True)
    planted = tree / "backend" / _PASTE_ARTIFACT_NAME
    planted.write_text("print('a copy')\n", encoding="utf-8")

    relative = f"backend/{_PASTE_ARTIFACT_NAME}"
    rule = paste_artifact_path_rule(relative)
    assert rule is not None, (
        f"{relative!r} is not flagged by the name rule, so the control "
        "would be planting something this gate does not reject"
    )

    result = _run_cli("--dir", str(tree))
    assert result.returncode == 1, (
        f"a tree holding a pasted copy returned {result.returncode}, not 1; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert f"[{rule}]" in result.stdout, (
        f"the report does not name the rule the module names ({rule!r}): "
        f"{result.stdout!r}"
    )
    assert f"{relative}:1:" in result.stdout, (
        f"the report does not say which file and line broke the rule: "
        f"{result.stdout!r}"
    )


def test_the_command_line_refuses_a_pasted_body_in_a_temporary_tree(tmp_path) -> None:
    """Negative control for the body rule: the name says nothing.

    A copy whose name was cleaned up before it was committed is caught
    by its body alone. The command has to reach the same answer, or the
    half of the rule it cannot see is a half that never fires.
    """
    tree = tmp_path / "tree"
    (tree / "backend").mkdir(parents=True)
    (tree / "backend" / "helper.py").write_text(_PASTE_DIFF_BODY, encoding="utf-8")

    result = _run_cli("--dir", str(tree))
    assert result.returncode == 1, (
        f"a tree holding a pasted diff body returned {result.returncode}, "
        f"not 1; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "[body-diff-fragment]" in result.stdout, (
        f"the report does not name the body rule: {result.stdout!r}"
    )
    assert "backend/helper.py:1:" in result.stdout, (
        f"the report does not say which file and line broke the rule: "
        f"{result.stdout!r}"
    )


def test_the_command_line_refuses_a_pasted_copy_in_the_index(tmp_path) -> None:
    """Negative control for the staged check: the index is what is committed.

    A file can be edited back to clean after it was staged, and the
    commit still carries what was staged — so the check has to read the
    index and not the working tree.

    The index is a throwaway copy: ``GIT_INDEX_FILE`` points at a file
    under ``tmp_path``, and the real index of this repository is never
    touched. The blob it carries is one that is already in this
    repository, so the control writes nothing into it.
    """
    index = tmp_path / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    _run_git(env, "read-tree", "HEAD")
    blob = _run_git(env, "rev-parse", "HEAD:backend/routes/verification.py").strip()
    _run_git(
        env,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},backend/{_PASTE_ARTIFACT_NAME}",
    )

    result = _run_cli("--check-staged", env=env)
    assert result.returncode == 1, (
        f"an index holding a pasted copy returned {result.returncode}, not 1; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert f"backend/{_PASTE_ARTIFACT_NAME}" in result.stdout, (
        f"the report does not name the staged file: {result.stdout!r}"
    )


def test_the_command_line_rejects_a_usage_error() -> None:
    """``2`` is a usage error, and it is distinct from ``1``.

    A gate that answers "1" for a mistake and "1" for a violation is a
    gate whose failure cannot be acted on: one means "fix the file",
    the other means "fix the command".
    """
    combined = _run_cli("--check-staged", "--dir", ".")
    assert combined.returncode == 2, (
        f"a contradictory invocation returned {combined.returncode}, not 2; "
        f"stdout={combined.stdout!r} stderr={combined.stderr!r}"
    )
    assert "--dir" in combined.stderr and "--check-staged" in combined.stderr, (
        "the refusal does not say which options contradict each other, so "
        f"the reader has to guess: {combined.stderr!r}"
    )

    unknown = _run_cli("--no-such-option")
    assert unknown.returncode == 2, (
        f"an unknown option returned {unknown.returncode}, not 2; "
        f"stderr={unknown.stderr!r}"
    )
    assert "no-such-option" in unknown.stderr, (
        "the refusal does not name the option it did not understand: "
        f"{unknown.stderr!r}"
    )


def test_the_command_line_runs_without_pytest_installed(tmp_path) -> None:
    """The hook runs under a bare interpreter, so the gate must import without pytest.

    ``scripts/check_no_paste_artifacts.py`` is called by the pre-commit
    hook as ``python3`` — no venv, no third-party packages. A gate module
    that imports pytest at the top level would make the hook fail with
    an ImportError on every machine whose interpreter has no pytest,
    which is exactly the machine the hook is supposed to protect.

    The control shadows pytest with a module that cannot be imported, so
    the command has to answer without it.
    """
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "pytest.py").write_text(
        "raise ModuleNotFoundError('pytest is not installed in this run')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(shadow)}

    result = _run_cli(env=env)
    assert result.returncode == 0, (
        f"the command returned {result.returncode} without pytest "
        f"installed; stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_the_pre_commit_config_wires_the_hook() -> None:
    """The hook is declared, and it is declared the way the others are.

    The hook id is what pre-commit prints when the hook fails, so a
    renamed id is a renamed gate; and the entry has to be the wrapper
    with ``--check-staged``, because a hook that scans the tracked tree
    would refuse every commit over a copy that is already in history.
    """
    import yaml

    document = yaml.safe_load(_PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = {
        hook["id"]: hook
        for repo in document["repos"]
        for hook in repo["hooks"]
    }
    assert "no-paste-artifacts" in hooks, (
        ".pre-commit-config.yaml no longer declares the paste-artifact "
        f"hook; it declares {sorted(hooks)!r}"
    )
    hook = hooks["no-paste-artifacts"]
    assert hook["language"] == "system", (
        f"the hook is not a system hook: {hook['language']!r}"
    )
    assert hook["stages"] == ["pre-commit"], (
        f"the hook does not run at the pre-commit stage: {hook['stages']!r}"
    )
    assert hook["pass_filenames"] is False, (
        "the hook reads the index itself, so it must not be handed a "
        "filename list to trust"
    )
    assert hook["entry"] == "python3 scripts/check_no_paste_artifacts.py --check-staged", (
        f"the hook does not call the wrapper in staged mode: {hook['entry']!r}"
    )


def test_the_wrapper_restates_no_rule() -> None:
    """The wrapper forwards; it does not carry a second copy of the rules.

    A second copy of a rule is a rule that can drift, and a drift here
    would be invisible: the gate would still be right, and the hook
    would be wrong.
    """
    source = _CLI_SCRIPT.read_text(encoding="utf-8")
    assert "test_no_paste_artifact_copies" in source, (
        "the wrapper does not import the gate module, so the rules it "
        "applies cannot be the ones the gate applies"
    )
    for needle in (
        "re.compile",
        "MARKER_RE",
        "STRUCTURAL_PASTE_RE",
        "HUNK_HEADER_RE",
        "完整",
        "（new",
    ):
        assert needle not in source, (
            f"the wrapper carries {needle!r}, so it holds a copy of the "
            "rules rather than forwarding to the module that owns them"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _directory_tree(root: Path) -> tuple[list[str], dict[str, str]]:
    """Return the (paths, bodies) of the files under *root*.

    The paths are relative to *root*, so a file that sits where a source
    file sits in this repository is seen here under the same name — which
    is what lets the structural rule, which is checked under the source
    roots, see what it is meant to see.

    Only the bodies a rule can ask about are read: a file whose
    extension cannot hold a pasted body and whose name says nothing is
    never examined, and reading it would cost time and memory for
    nothing.
    """
    root = root.resolve()
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            if full.is_symlink() or not full.is_file():
                continue
            paths.append(full.relative_to(root).as_posix())

    contents: dict[str, str] = {}
    for path in paths:
        if not (is_paste_artifact_path(path) or _has_pasteable_extension(path)):
            continue
        data = (root / path).read_bytes()
        if b"\x00" in data:
            # A binary blob is not a pasted diff. The content rule says
            # so too; reading it as text would only cost memory.
            continue
        contents[path] = data.decode("utf-8", errors="replace")
    return paths, contents


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_no_paste_artifacts",
        description=(
            "Refuse a pasted copy before the commit that carries it exists. "
            "Exits 0 clean, 1 on violations, 2 on a usage error."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-staged",
        action="store_true",
        help="check the files staged for the next commit",
    )
    mode.add_argument(
        "--check-tree",
        action="store_true",
        help="check the tracked tree (the default)",
    )
    parser.add_argument(
        "--dir",
        metavar="DIR",
        help="check this directory tree instead of the repository's tracked tree",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="the commit the tracked tree is read at (default: HEAD)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate and answer with the exit code the hook reads.

    ``0`` is a clean scan, ``1`` is at least one violation, and ``2`` is
    a usage error or a scan that could not run. The last is deliberately
    distinct from the first: "the gate found something" and "the gate did
    not run" need different responses, and collapsing them lets a broken
    gate look like a clean one.
    """
    args = _build_parser().parse_args(argv)
    if args.check_staged and args.dir:
        print(
            "error: --check-staged and --dir cannot be combined: the index "
            "is not a directory",
            file=sys.stderr,
        )
        return 2

    try:
        if args.dir:
            root = Path(args.dir)
            if not root.is_dir():
                print(f"error: not a directory: {args.dir}", file=sys.stderr)
                return 2
            paths, contents = _directory_tree(root)
            findings = paste_artifact_findings(paths, contents=contents)
            scope = f"the tree at {args.dir}"
        elif args.check_staged:
            paths = staged_paths()
            findings = paste_artifact_findings(
                paths, contents=blob_contents_at_index(paths)
            )
            scope = "the staged files"
        else:
            paths = tracked_paths_at_head(args.ref)
            findings = paste_artifact_findings(paths, ref=args.ref)
            scope = f"the tracked tree at {args.ref}"
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"ERROR: the scan could not run: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(f"FAIL no pasted copies: {len(findings)} violation(s) in {scope}")
        for finding in findings:
            print(f"  {finding.format()}")
        return 1

    print(f"OK  no pasted copies: 0 violation(s) in {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
