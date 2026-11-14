"""backend/plan_seed.py — the tracked seed plans, materialised into a plans root.

Why this module exists
----------------------
The two CI-contract seed plans — ``ci-contract-broken`` and
``ci-contract-valid`` — are the fixtures the CI-contract reader is
exercised against: one carries a contract that is not valid JSON, the
other one carries a contract that declares an exit code, a budget and a
summary format.

They used to be written by an inline Python heredoc embedded in a
verification plan's ``start_cmd``.  That put the only copy of them
inside plan *data*, which the repository does not track, and inside a
scratch directory the repository ignores.  A clean checkout therefore
had nothing to copy: the seed only existed on the machine that had run
that heredoc before.

The seed now lives in the repository twice over — as tracked fixture
files under ``backend/tests/fixtures/plans/`` and as this module — so
it can be reproduced anywhere, and so the service start path can put
the tracked bytes where the service serves them from.

What this module does
---------------------
1. Copies the three tracked files of each seed plan into
   ``<plans-root>/<plan-id>/``, byte for byte.
2. Writes the two state rows the plan's status is read from, with the
   same values the previous inline seed used: a ``plan_routing`` row in
   the ``verification`` phase and a ``plan_verification`` row that is
   ``pending`` at round 0 of 4.
3. Reads a service's **own** declaration — the environment its start
   command pins, and the seed ids that command names — so a caller
   knows which seeds a given service wants and where they belong.

What this module does not do
----------------------------
It does not touch any other plan in the target directory, and it does
not overwrite an existing state row — a second run is the same run as
the first.  The seeding is idempotent, so a service start can call it
unconditionally.

It does not import a service manager, either.  The caller that owns the
declaration owns the reading of it, and hands the readers in (see
:data:`EnvReader` and :data:`PathResolver`): a module that reached for
a service manager on its own would find the **harness'** copy — the
one that knows nothing about this project's declarations — and would
read a start command the wrong way.  That is not a theoretical risk:
it is exactly what happened when this module imported
``start_cmd_env`` and ``resolve_declared_data_path`` from
:mod:`service_manager` — the second of which exists in no harness at
all, so the import raised and the seed wrote nothing.

Where the module is used from
-----------------------------
``scripts/seed_ci_contract_plans.py`` is the command-line face of it,
and :mod:`service_manager` is the other one: the service start path
seeds the plans root the service's own declaration names, so a clean
checkout brings the two plan cards up without anybody running a script
by hand.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from config_paths import STATE_DB_ENV, resolve_state_db_path


#: Reads the environment a start command pins — the names and the
#: values it exports.
#:
#: The reading belongs to the caller: the service manager reads a start
#: command the way it tells two instances of the same program apart, and
#: the seed has to look at exactly that environment.  A reader of this
#: module's own would be a second reading — and two readings of
#: ``$(pwd)`` put the seed in a different directory than the service
#: serves.
EnvReader = Callable[[str], Mapping[str, str]]

#: Resolves a path a start command names against the directory the
#: command runs in.
#:
#: Same reasoning as :data:`EnvReader`: the caller hands the resolver
#: in, so the seed and the service read ``$(pwd)`` the same way.
PathResolver = Callable[[str, Path], Path]


#: The backend package root — derived from this file's own location, so
#: the tracked fixtures are found from any working directory and from a
#: clean checkout alike.
BACKEND_DIR: Path = Path(__file__).resolve().parent

#: The tracked fixtures the seed is copied from.
FIXTURE_ROOT: Path = BACKEND_DIR / "tests" / "fixtures" / "plans"

#: The plan ids the seed creates.
SEED_PLAN_IDS: Tuple[str, ...] = ("ci-contract-broken", "ci-contract-valid")

#: The files each seeded plan carries.
SEED_FILE_NAMES: Tuple[str, ...] = (
    "ci_contract.json",
    "verification_plan.json",
    "interview.json",
)

#: The phase a seeded plan starts in.
ROUTING_PHASE = "verification"

#: The verification status a seeded plan starts with.
VERIFICATION_STATUS = "pending"

#: The round a seeded plan starts at.
VERIFICATION_ROUND = 0

#: How many rounds a seeded plan is allowed.  The status endpoint reads
#: this back as ``verification_max_rounds``.
VERIFICATION_MAX_ROUNDS = 4

#: The environment variable that names the plans root a service serves.
#: The service's own declaration pins it; the seed reads it from there
#: and never from the process environment, so the seed lands in the
#: plans root *that service* serves.
PLANS_DIR_ENV = "PDT_PLANS_DIR"


@dataclass(frozen=True)
class SeedResult:
    """What one seeding run did.

    Attributes:
        plans_dir: the plans root that was seeded.
        plan_ids: the seed plan ids that were seeded.
        copied_files: the ``<plan-id>/<file-name>`` pairs that were
            copied, in the order they were written.
        routing_written: the plan ids whose routing row this run
            created.  A plan that already had one is not listed — the
            existing row is left alone.
        verification_written: the plan ids whose verification row this
            run created, under the same rule.
    """

    plans_dir: Path
    plan_ids: Tuple[str, ...]
    copied_files: Tuple[str, ...]
    routing_written: Tuple[str, ...]
    verification_written: Tuple[str, ...]


@dataclass(frozen=True)
class SeedTargets:
    """What a service's own declaration says about the seed.

    Attributes:
        plans_root: the plans root the service serves, or ``None`` when
            the declaration names no plans root.
        state_db_path: the state database the service serves, or
            ``None`` when the declaration names none.
        plan_ids: the seed plan ids the declaration names.  Empty means
            the service does not want the seed at all — and then not one
            byte may be written.
    """

    plans_root: Optional[Path]
    state_db_path: Optional[Path]
    plan_ids: Tuple[str, ...]


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def fixture_dir(plan_id: str) -> Path:
    """Return the tracked fixture directory of ``plan_id``."""
    return FIXTURE_ROOT / plan_id


def _seed_id_pattern(plan_id: str) -> re.Pattern[str]:
    """The pattern that recognises ``plan_id`` as a whole token.

    The boundary is spelled with a character class rather than ``\\b``:
    a seed id contains hyphens, and a word boundary would let a longer
    identifier (``xci-contract-broken``) count as a naming.
    """
    return re.compile(
        r"(?<![A-Za-z0-9_-])" + re.escape(plan_id) + r"(?![A-Za-z0-9_-])",
    )


def seed_plan_ids_named_in(start_cmd: str) -> Tuple[str, ...]:
    """The seed plan ids a start command names.

    A start command says which seeds it wants by naming them — the way
    the CI-contract declaration's inline seed names the two plan ids it
    writes.  A command that names none wants none: the caller then
    writes not one byte, which is what keeps a service that does not
    use the CI-contract seed exactly as it was.

    Args:
        start_cmd: the service's start command.

    Returns:
        The seed plan ids the command names, in registry order.
    """
    if not isinstance(start_cmd, str) or not start_cmd:
        return ()
    return tuple(
        plan_id for plan_id in SEED_PLAN_IDS
        if _seed_id_pattern(plan_id).search(start_cmd)
    )


# ---------------------------------------------------------------------------
# The service's own declaration
# ---------------------------------------------------------------------------


def _unquote(raw: str) -> str:
    """The value of an assignment, without the quotes around it."""
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _pinned_env(start_cmd: str, env_reader: EnvReader) -> Dict[str, str]:
    """The environment a start command pins.

    The reading is the caller's — the same one the service manager uses
    to tell two instances of the same program apart — so the seed looks
    at exactly the environment the service itself will see.
    """
    return dict(env_reader(start_cmd or ""))


def _declared_path(
    start_cmd: str, workdir: Path, env_name: str,
    env_reader: EnvReader, path_resolver: PathResolver,
) -> Optional[Path]:
    """Where a path the start command names actually is.

    The command's own ``$(pwd)`` is the directory it runs in — the
    work directory, not the process's — so a relative path is resolved
    against it and an absolute one is taken as it stands.  The
    resolution is the caller's, for the reason :data:`PathResolver`
    spells out.
    """
    raw = _unquote(_pinned_env(start_cmd, env_reader).get(env_name, ""))
    if not raw:
        return None
    return path_resolver(raw, Path(workdir))


def plans_root_from_start_cmd(
    start_cmd: str,
    workdir: Path,
    *,
    env_reader: EnvReader,
    path_resolver: PathResolver,
) -> Optional[Path]:
    """The plans root the start command names, or ``None``.

    ``None`` when the command names no plans root: there is then
    nowhere to put the seed, and the caller writes nothing.

    Args:
        start_cmd: the service's start command.
        workdir: the directory the command runs in.
        env_reader: reads the environment the command pins.
        path_resolver: resolves a named path against the work
            directory.

    Returns:
        The plans root, or ``None``.
    """
    return _declared_path(
        start_cmd, workdir, PLANS_DIR_ENV, env_reader, path_resolver,
    )


def state_db_from_start_cmd(
    start_cmd: str,
    workdir: Path,
    *,
    env_reader: EnvReader,
    path_resolver: PathResolver,
) -> Optional[Path]:
    """The state database the start command names, or ``None``.

    Args:
        start_cmd: the service's start command.
        workdir: the directory the command runs in.
        env_reader: reads the environment the command pins.
        path_resolver: resolves a named path against the work
            directory.

    Returns:
        The state database, or ``None``.
    """
    return _declared_path(
        start_cmd, workdir, STATE_DB_ENV, env_reader, path_resolver,
    )


def seed_targets(
    start_cmd: str,
    workdir: Path,
    *,
    env_reader: EnvReader,
    path_resolver: PathResolver,
) -> SeedTargets:
    """Everything a start command says about the seed.

    Args:
        start_cmd: the service's start command.
        workdir: the directory the command runs in — the declaration's
            working directory under the project directory.
        env_reader: reads the environment the command pins — the
            caller's own reading, so the seed looks at exactly the
            environment the service itself will see.
        path_resolver: resolves a named path against the work
            directory — the caller's own resolution, so the seed lands
            where the service serves.

    Returns:
        The plans root, the state database and the seed plan ids the
        command names.  The ids are empty when the command names none.
    """
    return SeedTargets(
        plans_root=plans_root_from_start_cmd(
            start_cmd, workdir,
            env_reader=env_reader, path_resolver=path_resolver,
        ),
        state_db_path=state_db_from_start_cmd(
            start_cmd, workdir,
            env_reader=env_reader, path_resolver=path_resolver,
        ),
        plan_ids=seed_plan_ids_named_in(start_cmd),
    )


# ---------------------------------------------------------------------------
# The seeding
# ---------------------------------------------------------------------------


def copy_seed_files(
    plans_dir: Path,
    plan_ids: Sequence[str] = SEED_PLAN_IDS,
) -> list[str]:
    """Copy the tracked fixture files of every seed into ``plans_dir``.

    The copy is byte for byte — the files are copied, not re-serialised,
    so a seeded plan is the fixture rather than a lookalike of it.  Only
    the seed plan directories are written; every other plan in
    ``plans_dir`` is left untouched.

    Args:
        plans_dir: the plans root to seed.
        plan_ids: the seed plan ids to copy.

    Returns:
        The ``<plan-id>/<file-name>`` pairs that were written.

    Raises:
        FileNotFoundError: a tracked fixture file is missing.
    """
    copied: list[str] = []
    for plan_id in plan_ids:
        source_dir = fixture_dir(plan_id)
        target_dir = Path(plans_dir) / plan_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in SEED_FILE_NAMES:
            source = source_dir / name
            if not source.is_file():
                raise FileNotFoundError(f"missing tracked seed file: {source}")
            shutil.copyfile(source, target_dir / name)
            copied.append(f"{plan_id}/{name}")
    return copied


def write_state_rows(
    state_db_path: Path,
    plan_ids: Sequence[str] = SEED_PLAN_IDS,
) -> tuple[list[str], list[str]]:
    """Write the routing and verification rows of every seed plan.

    The rows are the ones the previous inline seed wrote: a
    ``plan_routing`` row in the ``verification`` phase, and a
    ``plan_verification`` row that is ``pending`` at round 0 of 4.  An
    existing row is never overwritten, so seeding twice leaves the state
    exactly as the first run left it.

    Args:
        state_db_path: the state database to write into.
        plan_ids: the seed plan ids to write rows for.

    Returns:
        The plan ids whose routing row and verification row this run
        created.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import VerificationRepository

    conn = open_db(Path(state_db_path))
    try:
        migrate(conn)
        routing = RoutingRepository(conn)
        verification = VerificationRepository(conn)
        routing_written: list[str] = []
        verification_written: list[str] = []
        for plan_id in plan_ids:
            if routing.find(plan_id) is None:
                routing.insert(plan_id, ROUTING_PHASE)
                routing_written.append(plan_id)
            if verification.current(plan_id) is None:
                verification.insert(
                    plan_id,
                    VERIFICATION_STATUS,
                    round=VERIFICATION_ROUND,
                    max_rounds=VERIFICATION_MAX_ROUNDS,
                )
                verification_written.append(plan_id)
    finally:
        conn.close()
    return routing_written, verification_written


def seed_plans(
    plans_dir: Path,
    *,
    state_db_path: Optional[Path] = None,
    write_state: bool = True,
    plan_ids: Sequence[str] = SEED_PLAN_IDS,
) -> SeedResult:
    """Seed the CI-contract plans into ``plans_dir``.

    Args:
        plans_dir: the plans root to seed.  It is created if missing.
        state_db_path: the state database to write the plan rows into.
            ``None`` means :func:`config_paths.resolve_state_db_path` —
            the same resolver the server uses, so the operator's
            override is honoured.
        write_state: set to ``False`` to copy the plan files without
            touching the state database.
        plan_ids: the seed plan ids to seed.  Empty seeds nothing.

    Returns:
        A :class:`SeedResult` describing what was written.
    """
    plans_dir = Path(plans_dir)
    plan_ids = tuple(plan_ids)
    if not plan_ids:
        return SeedResult(
            plans_dir=plans_dir,
            plan_ids=(),
            copied_files=(),
            routing_written=(),
            verification_written=(),
        )
    plans_dir.mkdir(parents=True, exist_ok=True)
    copied = copy_seed_files(plans_dir, plan_ids)
    routing_written: list[str] = []
    verification_written: list[str] = []
    if write_state:
        db_path = Path(state_db_path) if state_db_path is not None else resolve_state_db_path()
        routing_written, verification_written = write_state_rows(db_path, plan_ids)
    return SeedResult(
        plans_dir=plans_dir,
        plan_ids=plan_ids,
        copied_files=tuple(copied),
        routing_written=tuple(routing_written),
        verification_written=tuple(verification_written),
    )
