"""Scope claims — who owns the file that failed?

The question this module answers
================================

Section 1 of the plan: when a repair round's gate fails on files that
**no task declared**, the round must not be judged dead outright. The
missing piece is the mapping "failing file -> the task that declares
it". Without it there is no way to tell *task 3 broke* from *nobody
owns this file*, and the second case is the one that has to reach the
refiner as work to do rather than as a verdict.

So the question is one question — *who owns this failing file?* — with
three answers, because "nobody" and "somewhere else entirely" are
different problems and must not be lumped together:

``claims``
    The failing file, and the ids of the tasks that declare it.
``unclaimed``
    The failing file is inside the project and **no** task declares it.
    This is the list the refiner has to be given: it is work nobody
    has claimed yet, not a failure of the task under repair.
``out_of_project``
    The failing file is not inside this plan's ``project_dir`` at all —
    another worktree, an absolute path elsewhere, a ``../`` escape. It
    is not this plan's business. Claiming it would be a lie, and
    refining on it would have the refiner edit a stranger's tree.

The three tables are exclusive
==============================

A file lands in exactly one of them. ``out_of_project`` is checked
first, because a path outside the project cannot be inside a
declaration: the declarations are read as project-relative paths, so
there is nothing for an outside path to match.

Pure function
=============

No I/O, no clock, no dependence on ``hash()`` seeds, and — this is the
part that matters for the containment check — **no filesystem access**.
The containment of a path is decided lexically: the path is split into
components, ``..`` is folded against the project root, and the result
is compared with the root's own components. A symlinked path that
leaves the project is therefore judged by its spelling, not by where
it really points; the conservative reading of "outside" is the right
one here, because the alternative is claiming a file this plan does
not own.

Reuse, not a second implementation
==================================

The case ids come from :func:`test_command_quality.failing_case_ids`
— the one place that already knows what a pytest ``FAILED`` line, an
``ERROR`` line and a pytest-timeout kill look like. This module does
not grow a second regex for the same output; it drops the ``::node``
suffix and asks the ownership question.

The ``files_to_modify`` sentinels come from :mod:`task`, so a task
that declared "I do not know which files" (or "I change no files")
claims nothing here — which is the honest answer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Dict, List, Sequence, Tuple

from task import NO_FILE_CHANGES_SENTINEL, UNKNOWN_MODIFICATIONS_SENTINEL
from test_command_quality import failing_case_ids

__all__ = [
    "failed_files_from_output",
    "claim_failed_files",
]


#: The two ``files_to_modify`` sentinels. A task carrying either one
#: declared **no** file, so it can claim nothing.
_SENTINELS = frozenset(
    list(NO_FILE_CHANGES_SENTINEL) + list(UNKNOWN_MODIFICATIONS_SENTINEL)
)


# ---------------------------------------------------------------------------
# path spelling — lexical, never touching the filesystem
# ---------------------------------------------------------------------------
def _split_path(path: str) -> Tuple[bool, List[str]]:
    """A path as ``(is_absolute, components)``.

    ``.`` and empty components are dropped — they say nothing about
    which file is meant. ``..`` is **kept**: it is the component that
    says "this path leaves the project", and folding it away here
    would throw that information out before the containment check
    gets to read it.
    """
    text = str(path or "").strip()
    is_absolute = text.startswith("/")
    components = [part for part in text.split("/") if part not in ("", ".")]
    return is_absolute, components


def _render_path(is_absolute: bool, components: Sequence[str]) -> str:
    """The inverse of :func:`_split_path`."""
    joined = "/".join(components)
    return "/" + joined if is_absolute else joined


def _canonical_path(path: str) -> str:
    """One spelling for one path.

    ``./`` is gone, a trailing slash is gone, a doubled separator is
    gone. ``backend//agent.py`` and ``./backend/agent.py`` are the same
    file, and a table that lists them twice is a table that says two
    files failed where one did.
    """
    is_absolute, components = _split_path(path)
    if not components:
        return "/" if is_absolute else "."
    return _render_path(is_absolute, components)


def _resolve_path(
    base_absolute: bool,
    base_components: List[str],
    path: str,
) -> Tuple[bool, List[str]]:
    """``path`` resolved against ``base``, lexically.

    ``..`` climbs the base and stops at its root; an absolute path
    ignores the base entirely. No filesystem is consulted, so the
    answer depends on the spelling of the path alone.
    """
    is_absolute, components = _split_path(path)
    if is_absolute:
        base_absolute, base_components = True, []
    resolved = list(base_components)
    for component in components:
        if component == "..":
            if resolved and resolved[-1] != "..":
                resolved.pop()
            elif not base_absolute:
                resolved.append("..")
            # An absolute path cannot climb above its own root.
        else:
            resolved.append(component)
    return base_absolute, resolved


def _project_root(project_dir: Any) -> Tuple[bool, List[str]]:
    """The project directory as ``(is_absolute, components)``.

    A relative project directory is resolved against the current
    directory, which is what every other spelling of "the project
    directory" in this codebase does.
    """
    text = str(project_dir or "").strip()
    is_absolute, components = _split_path(text)
    if is_absolute:
        return True, components
    cwd_absolute, cwd_components = _split_path(os.getcwd())
    return _resolve_path(cwd_absolute, cwd_components, text)


def _is_inside(base_components: List[str], resolved: List[str]) -> bool:
    """Whether a resolved path sits under the project root."""
    return resolved[: len(base_components)] == base_components


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
def _as_sequence(value: Any) -> List[Any]:
    """A list out of whatever the caller passed.

    A single string is **one** path, not a sequence of characters, and a
    single task dict is **one** task, not a sequence of its keys.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    return list(value)


def _task_field(task: Any, name: str) -> Any:
    """Read a field off a task, whether it is a dict or a ``SubTask``."""
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


def _declared_paths(
    tasks: Any,
    root_absolute: bool,
    root_components: List[str],
) -> List[Tuple[str, str]]:
    """``(task id, project-relative declared path)`` pairs.

    A declaration that points outside the project is dropped: it can
    claim nothing inside the project, and letting it claim by spelling
    would be the exact mistake this table exists to prevent.
    """
    declarations: List[Tuple[str, str]] = []
    for task in _as_sequence(tasks):
        task_id = str(_task_field(task, "id") or "")
        files = _task_field(task, "files_to_modify") or []
        if isinstance(files, str):
            files = [files]
        for declared in files:
            text = str(declared or "").strip()
            if not text or text in _SENTINELS:
                continue
            canonical = _canonical_path(text)
            if not canonical or canonical == ".":
                continue
            resolved_absolute, resolved = _resolve_path(
                root_absolute, root_components, canonical
            )
            if not resolved_absolute or not _is_inside(root_components, resolved):
                continue
            relative = _render_path(False, resolved[len(root_components):])
            if not relative:
                # A declaration of the project root itself declares no
                # file: it is a degenerate declaration, and reading it
                # as "claims everything" would hide the unclaimed work
                # this table exists to find.
                continue
            declarations.append((task_id, relative))
    return declarations


def _covers(declared: str, relative: str) -> bool:
    """Whether a declared path covers a failing file.

    A declaration covers the file it names, and — because a
    declaration may name a **directory** — every file below it. A
    declaration of the project root covers everything.
    """
    if not declared:
        return True
    if declared == relative:
        return True
    return relative.startswith(declared + "/")


# ---------------------------------------------------------------------------
# the public surface
# ---------------------------------------------------------------------------
def failed_files_from_output(run_output: str) -> List[str]:
    """The files a run output says failed, in order, without repeats.

    ``run_output`` is the raw output of a run. The case ids come from
    :func:`test_command_quality.failing_case_ids`, so a pytest
    ``FAILED`` line, an ``ERROR`` line and a pytest-timeout kill are
    all read the way the rest of the codebase reads them. The
    ``::node`` suffix is dropped, because the question here is about
    **files**: two cases failing in one file are one file to own.

    The spelling is canonicalised (no ``./``, no trailing slash, no
    doubled separator) so the same file written three ways is one
    entry.

    A pure function: no I/O, no exception, no output. An output that
    names no failing file yields ``[]`` — the caller reads that as
    "there is nothing to own", not as a failure.
    """
    files: List[str] = []
    seen = set()
    for case_id in failing_case_ids(run_output or ""):
        path = str(case_id or "").split("::", 1)[0].strip()
        if not path:
            continue
        canonical = _canonical_path(path)
        if not canonical or canonical == ".":
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        files.append(canonical)
    return files


def claim_failed_files(
    failed_files: Any,
    tasks: Any,
    project_dir: Any,
) -> Dict[str, Any]:
    """The ownership table for a set of failing files.

    Returns three tables, and a file is in exactly one of them:

    ``claims``
        ``{failing file: [task id, ...]}`` — the tasks that declare
        the file. A file declared by two tasks is claimed by both.
    ``unclaimed``
        The failing files that are inside the project and that **no**
        task declares. These are the ones the refiner has to be given:
        they are work nobody owns yet.
    ``out_of_project``
        The failing files that are not inside ``project_dir`` at all —
        an absolute path elsewhere, another worktree, a ``../``
        escape. They are neither claimed nor unclaimed: this plan does
        not own them, and the caller must not refine on them.

    The paths in ``claims`` and ``unclaimed`` are project-relative and
    canonical, so they can be compared with a task's
    ``files_to_modify`` and written into a plan artefact as they are.
    The paths in ``out_of_project`` keep their own spelling — an
    absolute path is the evidence that the failure came from
    somewhere else, and relativising it would throw that away.

    A pure function: no I/O, no exception, no output. Empty input
    gives three empty tables.
    """
    root_absolute, root_components = _project_root(project_dir)
    declarations = _declared_paths(tasks, root_absolute, root_components)

    claims: Dict[str, List[str]] = {}
    unclaimed: List[str] = []
    out_of_project: List[str] = []

    for raw in _as_sequence(failed_files):
        canonical = _canonical_path(raw)
        if not canonical or canonical == ".":
            continue
        resolved_absolute, resolved = _resolve_path(
            root_absolute, root_components, canonical
        )
        if not resolved_absolute or not _is_inside(root_components, resolved):
            if canonical not in out_of_project:
                out_of_project.append(canonical)
            continue
        relative = _render_path(False, resolved[len(root_components):])
        if not relative:
            # The project root itself is not a file that failed.
            continue
        owners: List[str] = []
        for task_id, declared in declarations:
            if task_id in owners:
                continue
            if _covers(declared, relative):
                owners.append(task_id)
        if owners:
            if relative not in claims:
                claims[relative] = owners
        elif relative not in unclaimed:
            unclaimed.append(relative)

    return {
        "claims": claims,
        "unclaimed": unclaimed,
        "out_of_project": out_of_project,
    }
