"""Necessity audit for the refiner — Q1–Q4, decided *before* a task runs.

Why this module exists
----------------------
The refiner used to go straight from "this task failed" to "split it".
Nothing in between asked whether the task was worth running at all.  Four
questions can be answered from task metadata and a project index alone —
no model, no database, no failed attempt to reason about:

==========  =====================================================
**Q1**      Is this task a duplicate of one already in the batch?
**Q2**      Can this task's acceptance be decided at all?
**Q3**      Does this task need an environment the project lacks?
**Q4**      Does this task's write domain collide with another's?
==========  =====================================================

Every one of those is a question about the *task*, not about the code it
tried to write.  Leaving them to the verification stage is what pollutes
the failure classification: a task that was a duplicate, or whose
acceptance could never be decided, produces a failure that the triage
cannot tell apart from a genuine deliverable defect, and it lands in the
same "same cause twice" bucket as a real one.

The audit runs first, and its verdict changes what the refiner is allowed
to do:

* **Q1** → the duplicate is *dropped*.  It is not split, not repaired,
  and it does not reach the executor.
* **Q2** → the split is *rejected*; the task goes back to the generation
  side to have its acceptance criterion fixed.  It is not dropped — a
  task with a broken acceptance criterion is still a task worth doing.
* **Q3** → the task is held as an *environment* problem.  It does not
  count as a same-cause failure, because the code was never the problem.
* **Q4** → the file-domain conflict is *resolved* (the two declarations
  are merged and the pair is serialised) or, when it cannot be, the
  task is *blocked*.

What "pure" means here
----------------------
``audit_tasks`` reads the caller's dictionaries and returns an answer.
It never writes to them: the caller's task list is the executor's
contract, and an audit that quietly edited it would be indistinguishable
from a task that had been rewritten.  The test
``test_audit_does_not_mutate_its_inputs`` pins that with a hash of the
input before and after the call.

The one thing the audit cannot do by itself is run
``pytest --collect-only`` — that is a process, and a process belongs to
the caller.  So the probe runner arrives through the project index
(``project_index["probe_runner"]``), exactly as in
:mod:`test_command_rules`, and the rules this module shares with the
repair generator are called rather than re-implemented:

* :func:`test_command_rules.collect_only_probe` — how many tests the
  acceptance command would collect;
* :func:`test_command_rules.is_empty_shell_test` — whether the test it
  names asserts anything at all;
* :func:`file_lock_manager.domains_overlap` — whether two write domains
  claim the same bytes, directory prefixes included.

The project index
-----------------
``project_index`` is a mapping describing the project the batch is
about to run against.  Every section is optional, and **a section that
is absent says nothing**: the audit then has no basis for a claim and
stays silent about that kind of requirement.  An explicitly empty
section is different — it means the project was asked and has nothing to
offer.

``files``
    Mapping of path → source text.  Used to read the test a task's
    acceptance command names, so that a shell test can be recognised.
``services`` / ``ports`` / ``env_vars``
    What the project provides.  A task's requirement that is not in the
    corresponding section is an environment failure (Q3).
``probe_runner``
    A callable taking the collection-only command and returning the
    number of collected items.  Without it the probe is not run, and no
    conclusion is drawn from it.

Why the rules are conservative
-------------------------------
A wrong verdict here is worse than no verdict.  A task dropped as a
duplicate that was not one loses its deliverable silently; a task
rejected as unverifiable that was perfectly verifiable is never fixed.
So every rule claims only what its inputs support:

* A title that is merely *alike* proves nothing.  Duplicates are
  recognised by the file domain and the acceptance command — the two
  things that decide whether two tasks write the same bytes and are
  judged the same way — with the title deciding only how the duplicate
  is *described*.
* A probe that collected nothing is a fact about the probe, so it is
  reported as "unverifiable" (the caller sends the task back) and never
  as "the test does not exist".
* A test file whose source the index does not carry cannot be judged,
  and one unjudgeable file does not make a whole task unverifiable.
* A requirement the index does not mention is unknown, not missing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from file_lock_manager import domains_overlap
from task import NO_FILE_CHANGES_SENTINEL, UNKNOWN_MODIFICATIONS_SENTINEL
from test_command_rules import collect_only_probe, is_empty_shell_test

__all__ = [
    "ACTION_BLOCK",
    "ACTION_DROP",
    "ACTION_ENV_INFRA",
    "ACTION_REJECT_SPLIT",
    "ACTION_SERIALIZE",
    "NecessityAudit",
    "NecessityFinding",
    "RULE_DOMAIN_CONFLICT",
    "RULE_DUPLICATE",
    "RULE_ENVIRONMENT",
    "RULE_UNVERIFIABLE",
    "audit_tasks",
    "command_source_paths",
]


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: The four questions, in the order the audit asks them.
RULE_DUPLICATE = "Q1"
RULE_UNVERIFIABLE = "Q2"
RULE_ENVIRONMENT = "Q3"
RULE_DOMAIN_CONFLICT = "Q4"

#: What the audit asks the caller to do about a finding.
ACTION_DROP = "drop"
ACTION_REJECT_SPLIT = "reject_split"
ACTION_ENV_INFRA = "env_infra"
ACTION_SERIALIZE = "serialize"
ACTION_BLOCK = "block"

#: The kinds of environment a task can require, and the noun a finding
#: uses to name one.  The second element is what a human reads.
_REQUIREMENT_KINDS: Tuple[Tuple[str, str], ...] = (
    ("services", "service"),
    ("ports", "port"),
    ("env_vars", "env var"),
)

#: Suffixes that make a command token a file path rather than a flag or
#: a program name.  A token with a ``/`` in it is a path whatever it
#: ends with.
_SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
        ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".rb", ".php",
        ".sh", ".sql", ".css", ".md",
    }
)

#: The pytest program name as it appears on a command line.  The
#: lookbehind keeps ``mypytest`` and ``foo.pytest`` out; the lookahead
#: keeps ``pytest-xdist`` out.
_PYTEST_TOKEN_RE = re.compile(r"(?<![\w.])pytest(?![\w\-])")


# ---------------------------------------------------------------------------
# The result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NecessityFinding:
    """One problem with one task.

    ``rule`` is the question that was asked (``Q1``–``Q4``), ``action``
    is what the audit asks the caller to do about it, and ``detail``
    says why.  The detail is what a human reads when a task disappears
    from a plan, so it names what was compared rather than restating
    the verdict.
    """

    rule: str
    task_id: str
    action: str
    detail: str


@dataclass
class NecessityAudit:
    """What the audit found, and what the caller has to do about it.

    ``droppable_task_ids``
        Tasks the audit says to drop (Q1).
    ``env_blocked_task_ids``
        Tasks held for an environment the project does not have (Q3).
        These are neither dropped nor rewritten — they wait.
    ``resolved_files``
        The merged write domain per task, for the pairs whose file
        conflict was resolved (Q4).  A task that is not in the mapping
        keeps the declaration it came with.
    """

    findings: List[NecessityFinding] = field(default_factory=list)
    droppable_task_ids: Set[str] = field(default_factory=set)
    env_blocked_task_ids: Set[str] = field(default_factory=set)
    resolved_files: Dict[str, List[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit_tasks(tasks: Any, project_index: Any = None) -> NecessityAudit:
    """Audit a batch of tasks for necessity.

    Args:
        tasks: The tasks to audit, in the order they are planned.  Order
            matters: for a duplicate, the *first* task of the pair is the
            one that survives, because it is the one the plan already
            promised to do.
        project_index: What the project provides — see the module
            docstring.  ``None`` and ``{}`` mean the same thing: the
            audit has nothing to compare against and says so by
            staying silent.

    Returns:
        The findings and the sets the caller acts on.  The inputs are
        not modified.
    """
    views = _build_views(tasks)
    index = _ProjectIndex(project_index)

    findings: List[NecessityFinding] = []
    droppable: Set[str] = set()
    env_blocked: Set[str] = set()
    merged_domains: Dict[str, Set[str]] = {}

    # -- Q1: is this task a duplicate of one already in the batch? ------
    for position, view in enumerate(views):
        match = _duplicate_of(view, views[:position])
        if match is None:
            continue
        earlier, detail = match
        findings.append(
            NecessityFinding(
                rule=RULE_DUPLICATE,
                task_id=view.task_id,
                action=ACTION_DROP,
                detail=detail,
            )
        )
        droppable.add(view.task_id)

    # -- Q2 and Q3: can this task be judged, and can it run here? ------
    for view in views:
        if view.task_id in droppable:
            # A duplicate is on its way out.  It is not also sent back
            # for a fix — there is nothing left to fix.
            continue

        reason = _unverifiable_reason(view, index)
        if reason is not None:
            findings.append(
                NecessityFinding(
                    rule=RULE_UNVERIFIABLE,
                    task_id=view.task_id,
                    action=ACTION_REJECT_SPLIT,
                    detail=reason,
                )
            )

        for detail in _environment_failures(view, index):
            findings.append(
                NecessityFinding(
                    rule=RULE_ENVIRONMENT,
                    task_id=view.task_id,
                    action=ACTION_ENV_INFRA,
                    detail=detail,
                )
            )
            env_blocked.add(view.task_id)

    # -- Q4: do two tasks write the same bytes? -----------------------
    #
    # A task that is being dropped, or that is held for the environment,
    # does not contend for the filesystem: it is not going to run.  It is
    # left out of the comparison so that a conflict is never reported
    # against a task that cannot act on it — and, more importantly, so
    # that a task held for the environment never acquires a resolved
    # domain it will not use.
    contenders = [
        view
        for view in views
        if view.task_id not in droppable and view.task_id not in env_blocked
    ]
    for earlier, later, shared in _domain_conflicts(contenders):
        names = ", ".join(sorted(shared))
        if _can_serialise(earlier, later, views):
            findings.append(
                NecessityFinding(
                    rule=RULE_DOMAIN_CONFLICT,
                    task_id=later.task_id,
                    action=ACTION_SERIALIZE,
                    detail=(
                        f"conflicts with task {earlier.task_id} on {names}; "
                        f"serialize after task {earlier.task_id}"
                    ),
                )
            )
            # One merged domain for both tasks.  Two different but
            # overlapping declarations lock two different lock files, so
            # the two tasks would each believe they hold the lock; one
            # shared declaration makes them contend for the same lock.
            merged = set(earlier.files) | set(later.files)
            merged_domains.setdefault(earlier.task_id, set()).update(merged)
            merged_domains.setdefault(later.task_id, set()).update(merged)
        else:
            findings.append(
                NecessityFinding(
                    rule=RULE_DOMAIN_CONFLICT,
                    task_id=later.task_id,
                    action=ACTION_BLOCK,
                    detail=(
                        f"conflicts with task {earlier.task_id} on {names}; "
                        f"cannot serialize, task {earlier.task_id} already "
                        f"depends on task {later.task_id}"
                    ),
                )
            )

    return NecessityAudit(
        findings=findings,
        droppable_task_ids=droppable,
        env_blocked_task_ids=env_blocked,
        resolved_files={
            task_id: sorted(paths) for task_id, paths in merged_domains.items()
        },
    )


# ---------------------------------------------------------------------------
# Q1 — duplicates
# ---------------------------------------------------------------------------


def _duplicate_of(
    view: "_TaskView", earlier_views: Sequence["_TaskView"]
) -> Optional[Tuple["_TaskView", str]]:
    """The earlier task ``view`` duplicates, and how it is described.

    Two tasks are the same work when they write the same domain and are
    judged by the same command.  The title decides only how the finding
    reads: an identical signature is reported as such, and a pair that
    merely agrees on domain and command is reported as that.

    A title that is *alike* proves nothing, and a task that does not
    know its own write domain cannot be shown to be a duplicate of
    anything, so neither produces a finding.
    """
    candidates = [
        other
        for other in earlier_views
        if view.domain_known
        and other.domain_known
        and frozenset(other.files) == frozenset(view.files)
        and other.command == view.command
    ]
    if not candidates:
        return None

    for other in candidates:
        if other.normalised_title == view.normalised_title:
            return other, "duplicate signature"
    return candidates[0], "duplicate file domain and command"


# ---------------------------------------------------------------------------
# Q2 — acceptance that cannot be decided
# ---------------------------------------------------------------------------


def _unverifiable_reason(view: "_TaskView", index: "_ProjectIndex") -> Optional[str]:
    """Why this task's acceptance cannot be decided, or ``None``.

    The three reasons, in the order they are checked:

    1. There is no acceptance criterion at all.
    2. The acceptance command collects nothing.
    3. The only test the command names asserts nothing.

    Reason 1 is about the *criterion*, not about the command.  A task
    that writes down what "done" looks like has an acceptance
    criterion even before a command runs it — refusing to split it
    would send a task with a perfectly judgeable acceptance back for a
    fix it does not need.  What is refused is a task whose acceptance
    is missing, or whose acceptance is a placeholder that says nothing.
    """
    if not view.command:
        if _is_judgeable_criteria(view.criteria):
            return None
        if view.criteria.strip():
            return "placeholder acceptance criteria"
        return "missing acceptance criteria"

    if index.probe_runner is not None and _is_pytest_command(view.command):
        try:
            collected = collect_only_probe(view.command, index.probe_runner)
        except Exception:
            # A probe that blew up measured nothing.  That is not
            # evidence that the task's tests do not exist, so nothing is
            # claimed.
            collected = None
        if collected == 0:
            return "probe collected no tests"

    sources = _known_test_sources(view.command, index.files)
    if sources and all(
        is_empty_shell_test(source) for source in sources.values()
    ):
        return "empty shell test"

    return None


#: The texts that stand in for an acceptance criterion without being
#: one.  A task whose acceptance is one of these says nothing about
#: what "done" looks like, so it is refused the split just like a task
#: that says nothing at all.
_PLACEHOLDER_CRITERIA = frozenset(
    {
        "todo", "tbd", "to be done", "to be determined", "fixme",
        "n/a", "na", "none", "tbc", "placeholder", "xxx", "???",
        "待补充", "待定", "待办", "无", "暂无", "略",
    }
)


def _acceptance_criteria(raw: Mapping[str, Any]) -> str:
    """The acceptance criterion the task writes down, if it writes one.

    ``acceptance_criteria`` is the field the plan carries — it holds
    the TDD-style specification of what "done" looks like.  A task
    that has one has an acceptance criterion even when no command has
    been attached to it yet.
    """
    return _text(raw.get("acceptance_criteria")).strip()


def _is_judgeable_criteria(criteria: str) -> bool:
    """Whether this acceptance text says what "done" looks like.

    A text that is empty, or that is only a placeholder, judges
    nothing: any outcome would satisfy it.  Anything else states an
    expectation, which is what an acceptance criterion is for.
    """
    text = (criteria or "").strip()
    if not text:
        return False
    bare = text.strip(" .。-—_").lower()
    if not bare:
        # Punctuation only — "…", "---", "。" says no more than nothing
        # does, so it judges nothing either.
        return False
    return bare not in _PLACEHOLDER_CRITERIA


def _is_pytest_command(command: str) -> bool:
    """Whether the command runs pytest at all.

    A command that is not a pytest command has no collection to probe,
    and asking a runner about it would answer a question nobody asked.
    """
    return bool(_PYTEST_TOKEN_RE.search(command))


def _known_test_sources(
    command: str, files: Mapping[str, str]
) -> Dict[str, str]:
    """The source of the test files the command names, when it is known.

    A file the index does not carry cannot be judged, so it is left out
    entirely rather than counted as an empty shell.
    """
    sources: Dict[str, str] = {}
    for token in _command_tokens(command):
        path = _normalise_path(token)
        if path is None:
            continue
        source = files.get(path)
        if isinstance(source, str) and source.strip():
            sources[path] = source
    return sources


def command_source_paths(command: str) -> Tuple[str, ...]:
    """The paths an acceptance command names, normalised.

    The audit cannot read the filesystem — it is a pure function — so
    the caller reads the files this command names and hands their
    sources back through the project index.  This is the list it reads:
    the same path-looking tokens the empty-shell check looks up, so the
    two always agree on which file a command means.
    """
    paths: List[str] = []
    for token in _command_tokens(command):
        path = _normalise_path(token)
        if path is not None and path not in paths:
            paths.append(path)
    return tuple(paths)


def _command_tokens(command: str) -> List[str]:
    """The file-path-looking tokens of a command line."""
    tokens: List[str] = []
    for raw in command.split():
        token = raw.strip("'\"`()[]{},;")
        if not token:
            continue
        # A pytest node id names the file and the test in it.
        token = token.split("::", 1)[0]
        if not token or not _looks_like_path(token):
            continue
        tokens.append(token)
    return tokens


def _looks_like_path(token: str) -> bool:
    """Whether a command token names a file rather than a flag."""
    if "/" in token:
        return True
    return os.path.splitext(token)[1].lower() in _SOURCE_SUFFIXES


# ---------------------------------------------------------------------------
# Q3 — the environment the task needs
# ---------------------------------------------------------------------------


def _environment_failures(view: "_TaskView", index: "_ProjectIndex") -> List[str]:
    """The requirements of this task the project cannot satisfy.

    A kind of requirement the project index does not mention is
    *unknown*, not missing: the audit has no basis for a claim about it
    and says nothing.
    """
    failures: List[str] = []
    for kind, label in _REQUIREMENT_KINDS:
        available = index.availability(kind)
        if available is None:
            continue
        for name in view.requires.get(kind, ()):
            if name not in available:
                failures.append(
                    f"{label} {name} is not available in the project index"
                )
    return failures


# ---------------------------------------------------------------------------
# Q4 — file domains
# ---------------------------------------------------------------------------


def _domain_conflicts(
    contenders: Sequence["_TaskView"],
) -> List[Tuple["_TaskView", "_TaskView", Set[str]]]:
    """The pairs whose write domains claim the same bytes.

    The earlier task of the pair is the one that survives, and the later
    one is the one that would have to be serialised after it.
    """
    conflicts: List[Tuple["_TaskView", "_TaskView", Set[str]]] = []
    for position, earlier in enumerate(contenders):
        for later in contenders[position + 1:]:
            shared = _shared_domains(earlier.files, later.files)
            if shared:
                conflicts.append((earlier, later, shared))
    return conflicts


def _shared_domains(files_a: Sequence[str], files_b: Sequence[str]) -> Set[str]:
    """The declared paths of two tasks that overlap.

    A directory domain (``src/a``) and a file written under it
    (``src/a/b.py``) are the same bytes, so the pair overlaps.  The
    returned set names the paths involved on both sides.
    """
    shared: Set[str] = set()
    for a in files_a:
        for b in files_b:
            if domains_overlap(a, b):
                shared.add(a)
                shared.add(b)
    return shared


def _can_serialise(
    earlier: "_TaskView", later: "_TaskView", views: Sequence["_TaskView"]
) -> bool:
    """Whether the pair can be ordered without closing a cycle.

    Serialising the pair means "the later task runs after the earlier
    one", which is a dependency edge from the later to the earlier.  If
    the earlier already depends on the later — directly or through
    other tasks — that edge closes a cycle, and a cycle has no order to
    serialise into.  The conflict is then blocked rather than resolved.

    Only the dependencies the batch itself declares are considered.  A
    dependency on a task outside the batch is a leaf: this function
    cannot know what that task depends on, and guessing would invent a
    cycle that may not exist.
    """
    edges = {view.task_id: view.depends_on for view in views}
    return not _reaches(earlier.task_id, later.task_id, edges)


def _reaches(
    start: str, target: str, edges: Mapping[str, Sequence[str]]
) -> bool:
    """Whether ``target`` is reachable from ``start`` along dependencies."""
    seen: Set[str] = set()
    stack: List[str] = list(edges.get(start, ()))
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


# ---------------------------------------------------------------------------
# The inputs, normalised
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TaskView:
    """A task, read the way the audit needs to read it.

    Every field is a *copy*.  The caller's dictionaries are never
    written to, and this view is what the rules compare.
    """

    task_id: str
    title: str
    normalised_title: str
    files: Tuple[str, ...]
    command: str
    criteria: str
    depends_on: Tuple[str, ...]
    requires: Dict[str, Tuple[str, ...]]
    domain_known: bool


def _build_views(tasks: Any) -> List[_TaskView]:
    """The batch, read into views.  Anything malformed is skipped."""
    if tasks is None or isinstance(tasks, (str, bytes, Mapping)):
        return []
    try:
        items = list(tasks)
    except TypeError:
        return []
    views: List[_TaskView] = []
    for position, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            continue
        views.append(_view_of(raw, position))
    return views


def _view_of(raw: Mapping[str, Any], position: int) -> _TaskView:
    """One task, normalised."""
    title = _text(raw.get("title"))
    files = _normalise_files(raw.get("files_to_modify"))
    return _TaskView(
        task_id=_text(raw.get("id")) or f"#{position}",
        title=title,
        normalised_title=_normalise_title(title),
        files=files,
        command=_effective_command(raw),
        criteria=_acceptance_criteria(raw),
        depends_on=_depends_on(raw),
        requires=_requirements(raw),
        domain_known=_domain_known(raw.get("files_to_modify")),
    )


def _normalise_title(title: str) -> str:
    """A title reduced to what two titles have in common.

    Case, punctuation and spacing are noise: "Fix the login flow" and
    "fix the login flow!" name the same piece of work.  The
    normalisation is used to *describe* a duplicate, never to find one
    on its own.
    """
    lowered = title.lower()
    cleaned = re.sub(r"[^\w]+", " ", lowered, flags=re.UNICODE)
    return " ".join(cleaned.split())


def _normalise_path(text: str) -> Optional[str]:
    """A declared path, normalised.  ``None`` for nothing usable."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    normalised = os.path.normpath(stripped)
    return normalised or None


def _normalise_files(raw: Any) -> Tuple[str, ...]:
    """The declared write domain: unique, normalised, in declared order."""
    files: List[str] = []
    seen: Set[str] = set()
    for value in _as_list(raw):
        if not isinstance(value, str):
            continue
        normalised = _normalise_path(value)
        if normalised is None or normalised in seen:
            continue
        seen.add(normalised)
        files.append(normalised)
    return tuple(files)


def _domain_known(raw: Any) -> bool:
    """Whether the task knows which files it writes.

    The two sentinels from :mod:`task` mean "this task does not know
    its own write domain" (or "it writes nothing"), not a path.  A task
    that does not know its domain cannot be shown to be a duplicate of
    another, and it has nothing to conflict with.
    """
    values = _as_list(raw)
    if (
        list(values) == list(UNKNOWN_MODIFICATIONS_SENTINEL)
        or list(values) == list(NO_FILE_CHANGES_SENTINEL)
    ):
        return False
    return True


def _effective_command(raw: Mapping[str, Any]) -> str:
    """The command that judges the task.

    ``test_command`` is the field the executor runs; ``test_commands``
    is the list a plan carries the same command in.  Whichever carries
    a command, that command is the acceptance criterion.
    """
    command = _text(raw.get("test_command"))
    if command:
        return _normalise_command(command)
    for candidate in _as_list(raw.get("test_commands")):
        text = _text(candidate)
        if text:
            return _normalise_command(text)
    return ""


def _normalise_command(command: str) -> str:
    """A command with its spacing collapsed."""
    return " ".join(command.split())


def _depends_on(raw: Mapping[str, Any]) -> Tuple[str, ...]:
    """The task ids this task waits for."""
    names: List[str] = []
    for value in _as_list(raw.get("depends_on")):
        text = _text(value)
        if text and text not in names:
            names.append(text)
    return tuple(names)


def _requirements(raw: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """What the task needs from the environment.

    A task may declare its requirements under ``requires`` or at the top
    level; both are read, and a requirement named twice is one
    requirement.
    """
    declared: Dict[str, List[str]] = {kind: [] for kind, _ in _REQUIREMENT_KINDS}

    nested = raw.get("requires")
    if isinstance(nested, Mapping):
        for kind, _ in _REQUIREMENT_KINDS:
            declared[kind].extend(_requirement_names(kind, nested.get(kind)))

    for kind, _ in _REQUIREMENT_KINDS:
        declared[kind].extend(_requirement_names(kind, raw.get(kind)))

    return {
        kind: tuple(dict.fromkeys(names)) for kind, names in declared.items()
    }


def _requirement_names(kind: str, value: Any) -> List[str]:
    """The requirement names of one kind, normalised."""
    if value is None:
        return []
    if isinstance(value, str):
        items: List[Any] = [value]
    elif isinstance(value, Mapping):
        items = list(value.keys())
    else:
        try:
            items = list(value)
        except TypeError:
            return []

    names: List[str] = []
    for item in items:
        name = _normalise_name(kind, item)
        if name:
            names.append(name)
    return names


def _normalise_name(kind: str, item: Any) -> str:
    """One requirement name, normalised the way the index normalises it.

    A port is a number whether it was written ``6379`` or ``"6379"``, so
    both spellings reach the same comparison.
    """
    if kind == "ports":
        try:
            return str(int(str(item).strip()))
        except (TypeError, ValueError):
            return str(item).strip()
    return str(item).strip()


# ---------------------------------------------------------------------------
# The project index
# ---------------------------------------------------------------------------


class _ProjectIndex:
    """What the project provides, read the way the audit needs it.

    A section the index does not carry is *absent*, and
    :meth:`availability` says so by returning ``None``.  The difference
    matters: an absent section means the audit has no information, an
    empty section means the project was asked and has nothing.
    """

    def __init__(self, raw: Any) -> None:
        self.files: Dict[str, str] = {}
        self.probe_runner: Optional[Callable[[str], int]] = None
        self._sections: Dict[str, Set[Any]] = {}

        if not isinstance(raw, Mapping):
            return

        files = raw.get("files")
        if isinstance(files, Mapping):
            for path, source in files.items():
                if not isinstance(path, str):
                    continue
                key = _normalise_path(path)
                if key is None:
                    continue
                self.files[key] = source

        for kind, _ in _REQUIREMENT_KINDS:
            if kind in raw:
                self._sections[kind] = _availability(kind, raw.get(kind))

        runner = raw.get("probe_runner")
        if callable(runner):
            self.probe_runner = runner

    def availability(self, kind: str) -> Optional[Set[Any]]:
        """What the project provides of one kind, or ``None`` if unknown."""
        return self._sections.get(kind)


def _availability(kind: str, value: Any) -> Set[Any]:
    """The names of one kind the project provides."""
    if value is None:
        # The section is present and empty: the project was asked and
        # has nothing of this kind.
        return set()
    if isinstance(value, str):
        items: List[Any] = [value]
    elif isinstance(value, Mapping):
        items = list(value.keys())
    else:
        try:
            items = list(value)
        except TypeError:
            return set()

    names: Set[Any] = set()
    for item in items:
        name = _normalise_name(kind, item)
        if name:
            names.add(name)
    return names


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """A value as text, or the empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> List[Any]:
    """A value as a list, without splitting a string into characters."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return list(value.values())
    try:
        return list(value)
    except TypeError:
        return []
