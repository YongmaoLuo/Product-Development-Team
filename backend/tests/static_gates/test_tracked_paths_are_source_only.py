"""The repository tracks source files — and nothing that is prose.

Why this gate exists
--------------------
Seven paths in this branch were not source. Six of them have *sentences*
for names: the headings of a delivery report, written to disk by a tool
that was handed prose where a file was asked for::

    backend/harness/mcode.py  (modified — full file is 1046 lines; only these parts changed)
    backend/tests/static_gates/test_coverage_floor.py （新建，634 行，全文）
    ` blocks back to disk verbatim. My previous message listed the two files with a *placeholder* ...

The seventh is a browser snapshot — ``.playwright-mcp/page-2026-10-24T16-51-29-898Z.yml``
— that a debugging tool wrote into the working tree and that a later
``git add -A`` published.

The first of those is the expensive one. ``backend/harness/mcode.py``
exists **twice** in the tree: the 1046-line adapter, and a file whose
*content* is the sentence above. Both are tracked. Nothing about either
is malformed — git lists them, the directory is full of real files, and
a reader cannot tell a stray artefact from a deliberate sample. The
shadow copy is the one that reads like a source file, because its name
starts with the name of a source file.

Deleting them is not a fix. The previous attempt deleted them and
nothing in the repository stopped the next one from growing back: the
gate that existed for this (``test_no_plan_artifacts_in_source_tree.py``)
looks only at the files *directly inside* ``backend/``, matches only nine
hard-coded plan-artefact names, and never looks at the repository root,
at ``.github/workflows/``, at ``.playwright-mcp/``, or at whether a name
contains a space.

So this gate reads the repository's own path list and asks five
questions about it.

What it pins
------------
1. **A tracked path is a path, not a sentence.** No tracked name may
   contain whitespace. A space in a filename is the fingerprint of a
   message that was written where a file was asked for, and no source
   file in this repository has one.
2. **A new path is source.** Every path this branch *adds* carries one
   of the extensions this repository publishes. The check is against the
   branch's own additions rather than the whole tree, because the tree
   legitimately holds extensions this branch has no business ruling on
   (``.lock``, ``.svg``, ``.coveragerc``).
3. **No ``delegate.sh`` is tracked here.** The delegation script lives
   in another repository; this branch is not allowed to carry a copy of
   it, and no extension rule would stop one.
4. **The backend's runtime dependencies do not move.** The mcode backend
   is a new *adapter*, not a new dependency: the PRD's scope boundary
   says the branch adds Python modules and one YAML mapping, so
   ``[project].dependencies`` must be item-for-item what ``main`` has.
5. **No code path writes an mcode ``config.yaml``.** The mcode backend
   reads a *profile map*; it does not generate, rewrite or take over
   mcode's own configuration.

What it does not check
----------------------
That the names are *meaningful* — a file called ``notes.txt`` passes
every rule here and may still be rubbish. That the tree has no stray
files at all: a copy of a real source file under a name with no
whitespace in it is invisible to a rule about names. And the
write-detection in rule 5 is narrower than the repository-wide scan in
``backend/tests/security/test_harness_credential_boundary.py``
(SEC-15), which walks every backend source with a deeper analysis of
what a call writes; this one is the scope-boundary half, and it names
the mcode surface explicitly so that a module which starts writing
mcode's configuration is reported against the feature that introduced
it.

Why the base ref is resolved rather than assumed
-------------------------------------------------
Rules 2 and 4 are about *this branch*, so they need a ref to compare
against. ``main`` is the branch this repository develops on, but a
checkout does not always have it: a CI runner that checked out a pull
request has the merge commit and nothing else. The resolution therefore
tries the spellings a base branch actually has, and — in CI, where the
remote is configured and the base branch is named in the environment —
fetches the base tree once so the comparison can still be made. When no
base can be resolved at all, the two rules that need it are **skipped
with a message saying so**, not passed: a rule that cannot run must not
read as a rule that ran.
"""

from __future__ import annotations

import ast
import os
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

import pytest

import source_scan

#: Repository root, taken from the walker the sibling gates use rather
#: than recomputed — the two have to agree.
_REPO_ROOT = source_scan.REPO_ROOT

#: Extensions a path this branch *adds* may carry. Everything the
#: repository publishes as source is one of these; a name with no
#: extension, or with one that only a screenshot, an archive or a
#: compiled artefact would have, is not something a source branch adds.
ALLOWED_NEW_EXTENSIONS = frozenset({
    ".cfg",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    # ``example/`` holds templates named ``<thing>.yaml.example``, and
    # the repository's own walker already counts ``.example`` as source
    # (``source_scan.SOURCE_EXTENSIONS``). A template a branch adds is
    # a source file, not a stray artefact.
    ".example",
})

#: Directories that a tool writes into and that are not part of the
#: repository: a browser-automation tool dumps a snapshot per
#: navigation, a later ``git add -A`` publishes it, and nothing reads it.
SCRATCH_PREFIXES = (".playwright-mcp/",)

#: The name this branch must not carry a copy of. The delegation script
#: belongs to another repository; the PRD's scope boundary says this
#: branch does not touch it.
DELEGATE_SCRIPT_NAME = "delegate.sh"

#: Refs this branch is compared against, in the order they are tried.
#: ``main`` is the branch this repository develops on; the ``origin/``
#: spellings are what a checkout that has the remote but no local
#: branch has.
_BASE_CANDIDATES = (
    "main",
    "origin/main",
    "refs/remotes/origin/main",
    "refs/heads/main",
)


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


def _git_bytes(*args: str) -> subprocess.CompletedProcess[bytes]:
    """Run *args* as git in the repository and return the raw result.

    Bytes, not text: a path is a byte string in git, and universal
    newline translation would rewrite a carriage return inside a path
    into a line feed — which is exactly the kind of character this gate
    is looking for.
    """
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        check=False,
    )


def _decode(raw: bytes) -> str:
    """Decode a git path, keeping bytes that are not valid UTF-8."""
    return raw.decode("utf-8", "surrogateescape")


def tracked_paths() -> list[str]:
    """Every path the index tracks, in git's own order.

    The index rather than ``HEAD``: the index is what the next commit
    will publish, so it is the moment a stray path becomes a defect.
    """
    result = _git_bytes("ls-files", "-z")
    assert result.returncode == 0, (
        "git ls-files failed ({}): {}".format(
            result.returncode, _decode(result.stderr).strip()
        )
    )
    return [path for path in _decode(result.stdout).split("\0") if path]


def _resolves(ref: str) -> bool:
    """Whether *ref* names a commit in this repository."""
    return (
        _git_bytes("rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(ref)).returncode
        == 0
    )


def _fetch_base_in_ci() -> None:
    """Fetch the base branch when CI checked out a shallow pull request.

    A pull-request runner has the merge commit and a depth-1 history, so
    ``main`` is not a ref it can name. The remote *is* configured there
    and the base branch is named in the environment, so one fetch of the
    base tree is enough for the comparison — a tree is all the two
    dot-range needs, and the three dot-range falls back to it when the
    shallow history cannot produce a merge base.

    Deliberately quiet: a fetch that fails must not fail the gate, it
    must only leave the two base-dependent rules with nothing to
    compare against, which they report as a skip.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    branch = os.environ.get("GITHUB_BASE_REF") or "main"
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "fetch",
                "--no-tags",
                "--depth=1",
                "origin",
                branch,
            ],
            capture_output=True,
            check=False,
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return


def base_ref() -> str | None:
    """The ref this branch's additions are measured against, or ``None``.

    ``None`` means no base could be resolved at all — the caller must
    treat that as "the comparison did not run", not as "there is
    nothing new".
    """
    for candidate in _BASE_CANDIDATES:
        if _resolves(candidate):
            return candidate
    _fetch_base_in_ci()
    for candidate in _BASE_CANDIDATES:
        if _resolves(candidate):
            return candidate
    return None


def _parse_name_status(raw: bytes) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status -z`` output into (status, path) pairs.

    With ``-z`` the fields are NUL-separated, so a path may contain
    anything at all — including the newline that a line-based parse
    would split on, which is precisely the shape of the artefacts this
    gate exists to catch.

    A rename or a copy names two paths, the one it came from and the
    one it became. Only the second is a path the branch introduces.
    """
    fields = raw.split(b"\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = _decode(fields[index])
        index += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            index += 1  # the path it came from
            path = _decode(fields[index])
            index += 1
            entries.append((status, path))
            continue
        path = _decode(fields[index])
        index += 1
        entries.append((status, path))
    return entries


def changed_paths(base: str) -> list[tuple[str, str]]:
    """Every path *base* and ``HEAD`` disagree about.

    The three-dot range (``base...HEAD``) answers "what did this branch
    add" — it compares against the branch point rather than against the
    tip of ``base``, so a commit that landed on ``base`` after the
    branch started is not reported as this branch's work. A shallow
    clone cannot compute a merge base, so the two-dot range is the
    fallback.
    """
    return _diff_entries(
        (
            ("{}...HEAD".format(base),),
            ("{}..HEAD".format(base),),
        )
    )


def staged_paths() -> list[tuple[str, str]]:
    """Every path the index and ``HEAD`` disagree about.

    The index is what the next commit publishes, so a change that has
    been staged but not committed is a change the repository is about to
    make — and a gate that only reads ``HEAD`` misses it until the
    commit that publishes it has already been reviewed.
    """
    return _diff_entries((("--cached", "HEAD"),))


def _diff_entries(specs: tuple[tuple[str, ...], ...]) -> list[tuple[str, str]]:
    """Run ``git diff --name-status -z`` with the first range that works."""
    result = None
    for spec in specs:
        result = _git_bytes("diff", "--name-status", "-z", *spec)
        if result.returncode == 0:
            return _parse_name_status(result.stdout)
    pytest.fail(
        "git diff failed for every range tried ({}): {}".format(
            ", ".join(" ".join(spec) for spec in specs),
            _decode(result.stderr).strip() if result is not None else "no result",
        )
    )


def _tree_paths(ref: str) -> set[str]:
    """Every path in the tree *ref* names."""
    result = _git_bytes("ls-tree", "-r", "--name-only", "-z", ref)
    if result.returncode != 0:
        return set()
    return {path for path in _decode(result.stdout).split("\0") if path}


def added_paths(base: str) -> list[str]:
    """The paths this branch adds and keeps.

    Three readings, combined:

    * ``base...HEAD`` — the additions the branch has committed. A path
      that was added and later deleted *inside* the branch does not
      appear in it at all: the range compares two trees, and the path is
      in neither.
    * ``--cached HEAD`` — the additions staged on top of that, and the
      deletions that undo a committed addition. A path the branch added
      and has since deleted in the working tree is not an addition any
      more: the next commit does not contain it, and neither does any
      clone of that commit.
    * the base's own tree — so that a path the base already has is
      never reported as this branch's addition, whichever of the two
      readings above happened to mention it.

    A rename's destination counts as added: the branch introduces that
    path even though the content came from somewhere else.
    """
    base_tree = _tree_paths(base)
    committed = {
        path
        for status, path in changed_paths(base)
        if status[0] in ("A", "R", "C") and path not in base_tree
    }
    staged = staged_paths()
    committed -= {path for status, path in staged if status[0] == "D"}
    staged_added = {
        path
        for status, path in staged
        if status[0] in ("A", "R", "C") and path not in base_tree
    }
    return sorted(committed | staged_added)


# ---------------------------------------------------------------------------
# The rules, as pure functions
# ---------------------------------------------------------------------------


def path_has_whitespace(path: str) -> bool:
    """Whether *path* contains a whitespace character."""
    return any(character.isspace() for character in path)


def extension_is_allowed(path: str) -> bool:
    """Whether *path*'s extension is one this repository publishes."""
    return PurePosixPath(path).suffix in ALLOWED_NEW_EXTENSIONS


def is_delegate_script(path: str) -> bool:
    """Whether *path* names a ``delegate.sh``."""
    return PurePosixPath(path).name == DELEGATE_SCRIPT_NAME


def is_snapshot_path(path: str) -> bool:
    """Whether *path* sits in a tool's scratch directory."""
    return any(path.startswith(prefix) for prefix in SCRATCH_PREFIXES)


def _require_base() -> str:
    """The base ref, or a skip that says the comparison did not run."""
    base = base_ref()
    if base is None:
        pytest.skip(
            "no base ref to compare against (tried: {}). The rules that "
            "compare this branch against its base did not run.".format(
                ", ".join(_BASE_CANDIDATES)
            )
        )
    return base


# ---------------------------------------------------------------------------
# Rule 1 — a tracked path is a path, not a sentence
# ---------------------------------------------------------------------------


def test_no_tracked_path_carries_whitespace() -> None:
    """No tracked name may contain a space, a tab or any other whitespace.

    Every one of the six residue paths this branch carried failed here.
    A name with whitespace in it is not a filename somebody chose; it is
    a sentence that reached the disk through a file-writing tool.
    """
    offenders = [path for path in tracked_paths() if path_has_whitespace(path)]
    assert not offenders, (
        "these tracked paths contain whitespace — they are prose that "
        "reached the disk through a file-writing tool, not source files. "
        "Delete them, and check whatever wrote them:\n  "
        + "\n  ".join(repr(path) for path in offenders)
    )


# ---------------------------------------------------------------------------
# Rule 2 — a new path is source
# ---------------------------------------------------------------------------


def test_every_added_path_carries_a_source_extension() -> None:
    """Every path this branch adds has an extension the repository publishes.

    Checked against the branch's own additions rather than the whole
    tree: the tree legitimately holds extensions this branch has no
    business ruling on, and a rule that reads the whole tree would be
    red for the wrong reason on any of them.
    """
    base = _require_base()
    offenders = [path for path in added_paths(base) if not extension_is_allowed(path)]
    assert not offenders, (
        "these paths are added by this branch and carry no source "
        "extension (allowed: {}):\n  ".format(
            ", ".join(sorted(ALLOWED_NEW_EXTENSIONS))
        )
        + "\n  ".join(repr(path) for path in offenders)
    )


# ---------------------------------------------------------------------------
# Rule 3 — no delegate.sh, no tool scratch space
# ---------------------------------------------------------------------------


def test_no_delegate_script_is_tracked() -> None:
    """The delegation script belongs to another repository.

    The PRD's scope boundary says this branch does not touch it. A copy
    would be invisible to the extension rule — ``delegate.sh`` is a
    perfectly good extension — so it needs its own rule.
    """
    offenders = [path for path in tracked_paths() if is_delegate_script(path)]
    assert not offenders, (
        "this repository must not track a delegate.sh — the delegation "
        "script belongs to the tool repository, and this branch is not "
        "allowed to carry a copy of it:\n  "
        + "\n  ".join(repr(path) for path in offenders)
    )


def test_no_tool_snapshot_is_tracked() -> None:
    """A debugging tool's scratch output is not part of the repository.

    ``.playwright-mcp/`` holds one snapshot per navigation, written into
    the working tree by the browser tool. A ``git add -A`` publishes
    them, and nothing ever reads them again.
    """
    offenders = [path for path in tracked_paths() if is_snapshot_path(path)]
    assert not offenders, (
        "tool snapshots are tracked — they are debugging output written "
        "into the working tree, not source. Delete them:\n  "
        + "\n  ".join(repr(path) for path in offenders)
    )


# ---------------------------------------------------------------------------
# Rule 4 — the runtime dependencies do not move
# ---------------------------------------------------------------------------


def _dependencies(document: bytes) -> list[str]:
    """The ``[project].dependencies`` list in a pyproject document."""
    parsed = tomllib.loads(document.decode("utf-8"))
    return list(parsed.get("project", {}).get("dependencies", []))


def test_the_backend_runtime_dependencies_are_unchanged() -> None:
    """``backend/pyproject.toml``'s dependency list is item-for-item what main has.

    The mcode backend is an adapter, not a dependency: the PRD's scope
    boundary limits the branch to Python modules and one YAML mapping, so
    a new runtime dependency is a change of scope that no review of the
    diff would necessarily catch — a dependency bump reads like an
    ordinary chore.
    """
    base = _require_base()
    original = _git_bytes("show", "{}:backend/pyproject.toml".format(base))
    assert original.returncode == 0, (
        "backend/pyproject.toml does not exist at {} — the comparison "
        "cannot be made".format(base)
    )
    current = (_REPO_ROOT / "backend" / "pyproject.toml").read_bytes()
    before = _dependencies(original.stdout)
    after = _dependencies(current)
    assert after == before, (
        "the backend's runtime dependencies differ from {}:\n  "
        "  added:   {}\n  removed: {}".format(
            base,
            [item for item in after if item not in before],
            [item for item in before if item not in after],
        )
    )


# ---------------------------------------------------------------------------
# Rule 5 — no code path writes an mcode config.yaml
# ---------------------------------------------------------------------------


#: Methods that write to the receiver: ``path.write_text(...)`` writes
#: to ``path``.
_RECEIVER_WRITERS = frozenset({
    "hardlink_to",
    "mkdir",
    "rmdir",
    "rmtree",
    "symlink_to",
    "touch",
    "unlink",
    "write",
    "write_bytes",
    "write_text",
    "writelines",
})

#: Callables whose *second* argument is the destination:
#: ``shutil.copy(src, dst)`` writes ``dst``, and ``yaml.safe_dump(data,
#: path)`` writes ``path``.
_DESTINATION_SECOND_ARGUMENT = frozenset({
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "dump",
    "move",
    "replace",
    "safe_dump",
})

#: Callables whose *first* argument is the destination when they are
#: called as a method: ``path.rename(dst)``.
_DESTINATION_FIRST_ARGUMENT = frozenset({"rename", "replace"})

#: How long a CI fetch of the base branch may take before it is given
#: up on.
_FETCH_TIMEOUT_SECONDS = 120


def _module_aliases(tree: ast.AST) -> set[str]:
    """The module names a file imports.

    This is what tells ``os.rename(src, dst)`` — which writes at the
    second argument — from ``path.rename(dst)``, which writes at the
    first one. The two have the same name and opposite arguments, and
    only the receiver says which is which.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases.add(alias.asname or alias.name)
    return aliases


def _config_yaml_names(tree: ast.AST) -> set[str]:
    """The module-level names that stand for a path ending in ``config.yaml``.

    A constant like ``BACKEND_CONFIG_YAML`` is a write target even
    though the string does not appear at the call site, so the scan has
    to know the name.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            name, value = node.target, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0], node.value
        else:
            continue
        if not isinstance(name, ast.Name) or value is None:
            continue
        for child in ast.walk(value):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "config.yaml" in child.value
            ):
                names.add(name.id)
                break
    return names


def _names_config_yaml(node: ast.AST, constants: set[str]) -> bool:
    """Whether an expression can be a path to a ``config.yaml``.

    The expression is followed through the names it uses: a path is very
    often built in one statement and written in the next, and a scan
    that only looked at the write call's own arguments would miss
    exactly that.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant):
            if isinstance(child.value, str) and "config.yaml" in child.value:
                return True
            continue
        if isinstance(child, ast.Name) and child.id in constants:
            return True
    return False


def _open_mode(node: ast.Call) -> str:
    """The mode an ``open(...)`` call asks for, or the empty string.

    ``open`` only counts as a write when it asks for a writing mode: the
    same call with ``"r"`` is a read, and a scan that counted it would
    report reads as writes.
    """
    mode = ""
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        if isinstance(node.args[1].value, str):
            mode = node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                mode = keyword.value.value
    return mode


def _write_targets(node: ast.Call, aliases: set[str]) -> list[ast.AST]:
    """The expressions a call writes to.

    Empty for a call that does not write to disk, or for one this scan
    does not recognise as a write — which is stated here rather than
    left implicit, because the repository-wide SEC-15 scan is the
    stricter half of this rule.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
        receiver = func.value
        if name in _RECEIVER_WRITERS:
            return [receiver]
        if name in _DESTINATION_FIRST_ARGUMENT:
            return list(node.args[:1])
        if name in _DESTINATION_SECOND_ARGUMENT and _is_module(receiver, aliases):
            return list(node.args[1:2])
        return []
    if isinstance(func, ast.Name):
        if func.id == "open":
            mode = _open_mode(node)
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                return list(node.args[:1])
            return []
        if func.id in _DESTINATION_SECOND_ARGUMENT:
            return list(node.args[1:2])
    return []


def _is_module(node: ast.AST, aliases: set[str]) -> bool:
    """Whether an expression is a module the file imported."""
    return isinstance(node, ast.Name) and node.id in aliases


def _config_yaml_writes(path: Path) -> list[str]:
    """Every write to a ``config.yaml`` in one module.

    Returns a list of human-readable locations; empty when the module
    writes none.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _module_aliases(tree)
    constants = _config_yaml_names(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for target in _write_targets(node, aliases):
            if _names_config_yaml(target, constants):
                offenders.append(
                    "{}:{}: {}".format(path, node.lineno, ast.unparse(target))
                )
    return offenders


def _mcode_surface() -> list[Path]:
    """Every first-party module under ``backend/`` that talks about mcode.

    Derived from the repository rather than from a list somebody has to
    remember to update: a new module that joins the feature joins the
    surface, and a module that stops talking about mcode leaves it.
    Tests are not first-party code — they are the cases, not the code
    the cases are about.
    """
    surface: list[Path] = []
    for relative in tracked_paths():
        if not relative.endswith(".py"):
            continue
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "backend":
            continue
        if "tests" in parts:
            continue
        path = _REPO_ROOT / relative
        if not path.is_file():
            continue
        if "mcode" not in path.read_text(encoding="utf-8").lower():
            continue
        surface.append(path)
    return sorted(surface)


def test_no_code_path_writes_an_mcode_config_yaml() -> None:
    """Nothing in the mcode feature writes a ``config.yaml``.

    The mcode backend reads a *profile map* — a mapping from a profile
    name to a provider directory. It does not generate, rewrite or take
    over mcode's own configuration, which is what the PRD's scope
    boundary says, and which is why this branch must not ship a code
    path that does it.

    This is the scope-boundary half of the rule; the repository-wide
    half lives in ``backend/tests/security/test_harness_credential_boundary.py``
    (SEC-15) and walks every backend source with a deeper analysis.
    """
    surface = _mcode_surface()
    assert surface, (
        "no module under backend/ mentions mcode — the surface this rule "
        "covers has become empty, so the rule would pass without checking "
        "anything."
    )
    offenders: list[str] = []
    for path in surface:
        offenders.extend(_config_yaml_writes(path))
    assert not offenders, (
        "a code path in the mcode feature writes a config.yaml:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Sensitivity — the rules above have to be able to fail
# ---------------------------------------------------------------------------


def test_the_rules_flag_the_shapes_they_exist_for() -> None:
    """Each rule has to reject the shape it was written for.

    A rule that cannot fail is a rule nothing enforces, and the shapes
    below are assembled here rather than read from the repository, so
    that the assertion is about the rule and not about the current
    tree — which, once the artefacts are deleted, contains none of them.
    """
    assert path_has_whitespace("backend/harness/mcode.py  (modified)")
    assert path_has_whitespace("backend/harness/mcode.py\t(modified)")
    assert not path_has_whitespace("backend/harness/mcode.py")

    assert not extension_is_allowed(
        "backend/harness/mcode.py  (modified — full file is 1046 lines)"
    )
    assert not extension_is_allowed("交付报告")
    assert extension_is_allowed("backend/harness/mcode.py")
    assert extension_is_allowed("example/mcode_profile_map.yaml.example")

    assert is_delegate_script("tools/delegate.sh")
    assert not is_delegate_script("backend/harness/registry.py")

    assert is_snapshot_path(".playwright-mcp/page-2026-10-24T16-51-29-898Z.yml")
    assert not is_snapshot_path("backend/tests/static_gates/source_scan.py")


def test_the_name_status_parser_survives_a_path_with_a_newline() -> None:
    """A path that contains a newline is parsed as one path, not two.

    The artefacts this gate exists for contain spaces and full-width
    punctuation; the next one may contain a newline, and a line-based
    parse would silently split it into two paths that each look
    innocent.
    """
    raw = b"A\0backend/harness/mcode.py\n(modified)\0M\0backend/cli.py\0"
    assert _parse_name_status(raw) == [
        ("A", "backend/harness/mcode.py\n(modified)"),
        ("M", "backend/cli.py"),
    ]
