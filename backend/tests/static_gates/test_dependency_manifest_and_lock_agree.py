"""The manifest, the lockfile, and the workflow must agree on one set.

Why this gate exists
--------------------
Three separate defects reached ``main`` through the same shape — a
dependency this repository installs was not the dependency anyone
declared, and nothing in the build noticed:

1. **Resolution drift.** ``backend/requirements.txt`` was a list of
   ``>=`` floors, so every CI job re-resolved the whole graph from
   scratch. ``filelock`` moved to 3.20.4, which registers a second
   ``atexit`` handler and breaks
   ``test_agent_no_atexit_settings.py``. ``main`` went red with no
   change to blame.
2. **Undeclared installs.** Twenty ``pip install`` lines in the
   workflow bypassed the file entirely and put *unpinned* copies of
   pytest itself into the venv, overriding whatever the file said.
   ``pytest-xdist`` was used by a lane running ``-n 2`` and was not
   declared anywhere at all — a resolver change could have removed it
   and the lane would have failed to start.
3. **Version disagreement.** ``backend/.venv`` sat at Python 3.9 while
   every CI job ran 3.11, for eleven days. The two answered the same
   test differently — a suite-level assertion passed on one and failed
   on the other — so a local result and a CI result were not results
   about the same thing.

What this pins
--------------
The manifest moved to ``backend/pyproject.toml`` with
``backend/uv.lock`` beside it on 2026-10-06, and the three fixes are
mechanical. What is *not* mechanical is keeping them true, because
each file can now drift from the others silently:

1. **Manifest ↔ lock.** ``uv.lock`` records the requirement strings
   the manifest gave it under ``[package.metadata] requires-dist``.
   If that no longer matches ``backend/pyproject.toml``, somebody
   edited the manifest without re-locking, and CI is about to install
   a package set nobody declared. ``uv sync --locked`` would also
   catch this — but only on a job that runs it, and only as a uv
   error. This says the same thing, in the manifest's own terms, and
   it needs uv installed to run.
2. **Every declared name is locked.** A requirement can be present in
   ``pyproject.toml`` and absent from the lock's package table — a
   hand-edited lock, or a marker/extras spelling uv resolved away.
   Then ``uv sync --locked`` succeeds and installs nothing for it,
   which is the "collection fails silently" shape again.
3. **Python version agreement.** ``requires-python`` in the manifest,
   the normalised form ``uv.lock`` records, and every
   ``python-version:`` in the workflows must name one interpreter
   family. A disagreement here is the eleven-day defect, waiting to
   recur.
4. **No install crept back in.** The workflow must route every
   backend install through ``uv sync --locked``. This is the direct
   guard on defect 2 above: a contributor who adds one convenient
   ``pip install`` gets a red build instead of an unpinned package.

Scope, stated honestly
----------------------
This reads files; it does not resolve anything. A lockfile that is
*internally* consistent but pins a version with a yanked release is
not something this can see — ``uv sync --locked`` is what catches
that, and CI runs it. Conversely this gate runs where uv may not be
installed at all, and a gate that only works on one machine is not a
gate.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

# Repository layout — this file lives at
# ``backend/tests/static_gates/``, so the root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO_ROOT / "backend" / "pyproject.toml"
_LOCK = _REPO_ROOT / "backend" / "uv.lock"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_JSON_CLEANUP_YML = _REPO_ROOT / ".github" / "workflows" / "test_json_cleanup.yml"


def _load_manifest() -> dict:
    with _MANIFEST.open("rb") as fh:
        return tomllib.load(fh)


def _load_lock() -> dict:
    with _LOCK.open("rb") as fh:
        return tomllib.load(fh)


def _canonical(name: str, specifier: str | None) -> tuple[str, str]:
    """Return the ``(normalised name, specifier)`` pair uv records.

    PEP 503 normalisation — runs of ``-``/``_``/``.`` collapse to a
    single ``-`` and the name is lowercased — is what uv writes into
    ``requires-dist``, so the manifest side has to be folded the same
    way before the two can be compared as strings.
    """
    key = re.sub(r"[-_.]+", "-", name).lower()
    return key, specifier or ""


def _declared_requirements(manifest: dict) -> dict[tuple[str, str], str]:
    """Every requirement the manifest declares, from both groups.

    Returns a mapping of ``(normalised name, specifier)`` to the
    group the requirement came from, so a failure can say *which*
    list lost it.
    """
    out: dict[tuple[str, str], str] = {}
    project = manifest.get("project", {})
    for raw in project.get("dependencies", []):
        name, spec = _split_requirement(raw)
        out[_canonical(name, spec)] = "project.dependencies"
    for group, entries in manifest.get("dependency-groups", {}).items():
        for raw in entries:
            name, spec = _split_requirement(raw)
            key = _canonical(name, spec)
            # A package declared in both groups is a mistake worth
            # naming, but it is not this gate's job to reject it —
            # uv accepts it. Record the first group and move on.
            out.setdefault(key, f"dependency-groups.{group}")
    return out


def _split_requirement(raw: str) -> tuple[str, str]:
    """Split a PEP 508 requirement string into ``(name, specifier)``.

    Deliberately not a full parser: the manifest is hand-written and
    carries no URLs or markers, so the only two shapes that occur are
    ``name`` and ``name<specifier>``. Anything else raises, so a
    requirement that grows a marker cannot slip past a comparison that
    silently ignored the part it did not understand.
    """
    text = raw.strip()
    match = re.match(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<spec>[<>=!~;\[].*)?$",
        text,
    )
    if match is None:
        raise AssertionError(
            f"cannot parse requirement {raw!r} in {_MANIFEST.name}; this "
            f"gate only handles `name` and `name<specifier>` forms, and "
            f"reading a requirement it cannot parse is how a comparison "
            f"ends up silently ignoring half the string."
        )
    return match.group("name"), (match.group("spec") or "").strip()


def _locked_requirements(lock: dict) -> dict[tuple[str, str], str]:
    """The requirement set ``backend/uv.lock`` says it was built from.

    Read from the root package's metadata rather than from the
    ``[[package]]`` table: the table records what got *installed* (a
    flattened, extras-expanded graph) while the metadata records what
    the manifest *asked for*. Only the second one answers "did the lock
    come from the manifest".

    uv splits the two declarations across sibling tables —
    ``requires-dist`` for ``project.dependencies`` and
    ``requires-dev`` for the dependency groups — so both are read, and
    the group name is carried through so a failure can say which list
    lost the requirement.
    """
    for package in lock.get("package", []):
        metadata = package.get("metadata")
        if metadata is None:
            continue
        out: dict[tuple[str, str], str] = {}
        for entry in metadata.get("requires-dist", []):
            out[_canonical(entry["name"], entry.get("specifier"))] = (
                "project.dependencies"
            )
        for group, entries in metadata.get("requires-dev", {}).items():
            for entry in entries:
                out.setdefault(
                    _canonical(entry["name"], entry.get("specifier")),
                    f"dependency-groups.{group}",
                )
        return out
    raise AssertionError(
        f"{_LOCK.name} carries no [package.metadata] table; uv has not "
        f"recorded which requirements it resolved, so this gate cannot "
        f"tell a lock that matches {_MANIFEST.name} from one that does "
        f"not."
    )


# ---------------------------------------------------------------------------
# 1 — manifest ↔ lock
# ---------------------------------------------------------------------------


def test_the_lock_was_built_from_the_manifest() -> None:
    """``uv.lock`` must record exactly the requirements the manifest declares.

    The failure this catches is a contributor editing
    ``backend/pyproject.toml`` — adding a dependency, or moving one
    between the runtime list and the ``dev`` group — and not running
    ``uv lock``. CI's ``uv sync --locked`` fails on that too, but as
    an opaque resolver error pointing at uv; this names the manifest
    and the group, which is the part a reader actually needs.
    """
    declared = _declared_requirements(_load_manifest())
    locked = _locked_requirements(_load_lock())

    missing = {
        key: group for key, group in declared.items() if key not in locked
    }
    assert not missing, (
        "these requirements are in backend/pyproject.toml but not in "
        "backend/uv.lock, so CI would install without them:\n"
        + "\n".join(
            f"  {name}{spec or ''}  (declared in {group})"
            for (name, spec), group in sorted(missing.items())
        )
        + "\nRun `uv lock --project backend` and commit the result."
    )

    extra = {key for key in locked if key not in declared}
    assert not extra, (
        "these requirements are in backend/uv.lock but no longer in "
        "backend/pyproject.toml, so the lock is installing something "
        "the manifest does not claim:\n"
        + "\n".join(f"  {name}{spec}" for name, spec in sorted(extra))
        + "\nRun `uv lock --project backend` to bring the lock back in "
          "line with the manifest."
    )


# ---------------------------------------------------------------------------
# 2 — every declared name is actually locked
# ---------------------------------------------------------------------------


def test_every_declared_package_appears_in_the_lock_table() -> None:
    """A declared requirement must have an installable entry, not just a mention.

    ``requires-dist`` records intent; ``[[package]]`` records what can
    actually be installed. A requirement can sit in the first without
    appearing in the second — uv emits one for a package it resolved
    only as an extra, and a hand-edited lock can drop an entry
    outright. Then ``uv sync --locked`` succeeds, installs nothing for
    the requirement, and the failure surfaces as a collection error
    naming some unrelated import.
    """
    manifest = _load_manifest()
    lock = _load_lock()

    locked_names = {
        re.sub(r"[-_.]+", "-", pkg["name"]).lower() for pkg in lock["package"]
    }

    missing: list[str] = []
    for (name, spec), group in _declared_requirements(manifest).items():
        if name not in locked_names:
            missing.append(f"  {name}{spec}  (declared in {group})")

    assert not missing, (
        "backend/pyproject.toml declares requirements with no entry in "
        "the uv.lock package table, so `uv sync --locked` installs "
        "nothing for them:\n" + "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# 3 — one interpreter, three files
# ---------------------------------------------------------------------------


def test_every_workflow_runs_the_interpreter_the_manifest_declares() -> None:
    """``requires-python``, ``uv.lock`` and every ``python-version:`` must name one family.

    The manifest is the declaration; the lock records the narrowed form
    uv resolved it to; the workflows record what the runners actually
    start. When those three disagree, a local run and a CI run are
    answers to different questions — and the disagreement is invisible
    precisely because each file looks correct on its own.
    """
    declared = _load_manifest()["project"]["requires-python"]
    locked = _load_lock()["requires-python"]

    lower = _manifest_minor(declared)
    assert _lock_minor(locked) == lower, (
        f"backend/pyproject.toml declares requires-python={declared!r} "
        f"(i.e. Python {lower}) but backend/uv.lock records "
        f"requires-python={locked!r}. The lock was resolved for a "
        f"different interpreter than the manifest asks for; run "
        f"`uv lock --project backend` on the declared version."
    )

    wanted = ".".join(lower.split(".")[:2])  # "3.11"
    for workflow in (_CI_YML, _JSON_CLEANUP_YML):
        if not workflow.exists():
            continue
        for job_name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
            for step in job.get("steps", []):
                if not str(step.get("uses", "")).startswith("actions/setup-python"):
                    continue
                version = str(step.get("with", {}).get("python-version", "")).strip('"')
                assert version == wanted, (
                    f"{workflow.name}: job {job_name!r} sets "
                    f"python-version {version!r}, but "
                    f"backend/pyproject.toml declares {declared!r}. The "
                    f"lane would test a different interpreter than the "
                    f"manifest promises, which is the defect that let a "
                    f"3.9 venv and a 3.11 runner disagree for eleven "
                    f"days. Change the job or the manifest, not both "
                    f"silently."
                )


def _manifest_minor(specifier: str) -> str:
    """Return the ``M.m`` an ``>=M.m,<M.(m+1)``-style specifier names.

    Deliberately simple: the repository pins one minor series and the
    gate exists to notice the files disagreeing, not to implement PEP
    440. A specifier this cannot read raises, because a floor that
    silently reads as "no constraint" would turn this gate into the
    permanent green it was written to prevent.
    """
    match = re.match(
        r"^>=(?P<low>\d+\.\d+)(?:\.\d+)?\s*,\s*<(?P<high>\d+\.\d+)", specifier
    )
    if match is None:
        raise AssertionError(
            f"requires-python={specifier!r} is not the "
            f"`>=M.m,<M.(m+1)` form this gate knows how to compare. It "
            f"was written for a repository that pins exactly one minor "
            f"series; if the pin really changed, update this gate in "
            f"the same commit rather than leaving it reading a shape "
            f"it does not understand."
        )
    low, high = match.group("low"), match.group("high")
    assert high.startswith(low.rsplit(".", 1)[0] + "."), (
        f"requires-python={specifier!r} spans more than one minor "
        f"series; this gate compares a single one."
    )
    return low


def _lock_minor(specifier: str) -> str:
    """Return the ``M.m`` out of uv's normalised ``==M.m.*`` form.

    uv rewrites a manifest range into the exact series it resolved
    for, so the two files never carry the same string — comparing them
    literally would report a mismatch on a tree that is in perfect
    agreement. The check is that both name the same series.
    """
    match = re.match(r"^==(?P<minor>\d+\.\d+)\.\*$", specifier)
    if match is None:
        raise AssertionError(
            f"requires-python={specifier!r} in {_LOCK.name} is not the "
            f"`==M.m.*` form this gate knows how to compare. uv narrows a "
            f"manifest range to the series it resolved, so that is the "
            f"only shape a lock produced by `uv lock` carries; if the "
            f"tool changed, update this gate in the same commit."
        )
    return match.group("minor")


# ---------------------------------------------------------------------------
# 4 — no install path crept back in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workflow",
    [_CI_YML, _JSON_CLEANUP_YML],
    ids=lambda p: p.name,
)
def test_no_workflow_installs_a_package_outside_the_lock(workflow: Path) -> None:
    """Only the uv bootstrap may run pip; nothing may install a named package.

    The one legitimate pip use is installing uv itself, pinned, on the
    runner that has no other way to get it. Every other install is a
    package arriving from outside ``backend/uv.lock`` — the shape that
    let eight lanes carry an unpinned ``requests`` and two carry a
    ``pytest-xdist`` nothing declared.
    """
    if not workflow.exists():
        pytest.skip(f"{workflow.name} is not present")

    offenders: list[str] = []
    for lineno, line in enumerate(workflow.read_text().splitlines(), start=1):
        stripped = line.strip()
        if "pip install" not in stripped:
            continue
        if stripped.endswith("pip install --upgrade pip"):
            continue
        if re.search(r"pip install uv==\d", stripped):
            continue
        offenders.append(f"  {workflow.name}:{lineno}: {stripped}")

    assert not offenders, (
        "these lines install a package outside backend/uv.lock, which "
        "is the defect class this manifest exists to end:\n"
        + "\n".join(offenders)
        + "\nDeclare the package in backend/pyproject.toml, re-run "
          "`uv lock --project backend`, and let `uv sync --locked` "
          "install it."
    )


def test_the_requirements_file_is_gone() -> None:
    """``backend/requirements.txt`` must not come back alongside the manifest.

    Two manifests is one too many: only one of them is covered by
    ``uv.lock``, and the repository has already lost packages twice to
    the one that is not. Keeping the old file around as a "convenience"
    export would restore exactly that ambiguity.
    """
    legacy = _REPO_ROOT / "backend" / "requirements.txt"
    assert not legacy.exists(), (
        "backend/requirements.txt exists again. backend/pyproject.toml "
        "is the manifest and backend/uv.lock is what CI installs; a "
        "requirements file beside them is a second source of truth "
        "that nothing keeps in sync. If an export is needed, generate "
        "it with `uv export` at use time rather than committing it."
    )


# ---------------------------------------------------------------------------
# 5 — what the install path change moved
# ---------------------------------------------------------------------------

#: Console scripts this project's own dependencies provide. Before the
#: manifest moved, ``pip install -r backend/requirements.txt`` ran
#: against the ``actions/setup-python`` environment, so these resolved
#: bare from ``PATH`` in every job. ``uv sync`` installs into
#: ``backend/.venv`` instead, so a bare invocation now finds nothing —
#: and a lane that calls one fails with "command not found" rather
#: than with anything that points at the install.
_PROJECT_CONSOLE_SCRIPTS = ("bandit", "coverage", "pytest")


@pytest.mark.parametrize(
    "workflow",
    [_CI_YML, _JSON_CLEANUP_YML],
    ids=lambda p: p.name,
)
def test_project_console_scripts_go_through_the_project_venv(workflow: Path) -> None:
    """A dependency's console script is invoked as ``./.venv/bin/<tool>``.

    ``uv sync --project backend`` builds ``backend/.venv`` and installs
    into it. The ``actions/setup-python`` environment it runs alongside
    is deliberately left holding nothing but uv itself, so a step that
    calls one of these tools bare resolves against whatever happens to
    be on the runner's ``PATH`` — which, for a job that installs
    nothing, is nothing at all.

    The failure this prevents is the awkward kind: it is not an
    assertion, it is a lane that dies before pytest runs, so it reads
    as an infrastructure outage rather than as a dependency problem.
    """
    if not workflow.exists():
        pytest.skip(f"{workflow.name} is not present")

    # Only a ``run:`` payload executes anything. Reading the workflow
    # through the YAML parser rather than as text is what keeps a job
    # *named* ``coverage-gate`` or a ``# bandit`` comment from being
    # mistaken for a command — and a gate that has to be second-guessed
    # by hand is a gate that gets deleted the next time it is
    # inconvenient.
    #
    # A bare invocation is one where the tool *is* the command being run:
    # the first word of a line, or the first word after a shell
    # separator. Anything with a path in front of it
    # (``./.venv/bin/bandit``) or an interpreter in front of it
    # (``./.venv/bin/python3 -m coverage``) is the qualified form this
    # gate is asking for, and both are left alone.
    _STARTS_A_COMMAND = re.compile(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
        r"(?:.*?(?:&&|\|\||;|\||\()\s+)?"
        rf"(?P<tool>{'|'.join(re.escape(t) for t in _PROJECT_CONSOLE_SCRIPTS)})\b"
    )

    offenders: list[str] = []
    for job_name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for raw in run.splitlines():
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if _STARTS_A_COMMAND.match(stripped):
                    offenders.append(
                        f"  {workflow.name}: job {job_name!r}, "
                        f"step {step.get('name', '<unnamed>')!r}: {stripped}"
                    )

    assert not offenders, (
        "these lines call a console script that `uv sync` installs into "
        "backend/.venv but never puts on PATH. Route them through "
        "`./.venv/bin/<tool>` (or `./.venv/bin/python3 -m <tool>`) the "
        "way the pytest lanes already do:\n"
        + "\n".join(offenders)
    )