#!/usr/bin/env python3
"""scripts/seed_ci_contract_plans.py — seed the CI-contract plans into a plans root.

Why this script exists
----------------------
The two CI-contract seed plans — ``ci-contract-broken`` and
``ci-contract-valid`` — are the fixtures the contract reader is
exercised against: one carries a contract that is not valid JSON, the
other carries a contract that declares an exit code, a budget and a
summary format.

They used to be written by an inline Python heredoc embedded in a
verification plan's ``start_cmd``.  That put the only copy of them
inside plan *data*, which the repository does not track, and inside a
scratch directory the repository ignores.  A clean checkout therefore
had nothing to copy: the seed only existed on the machine that had
run that heredoc before.

The seed now lives in the repository twice over — as tracked fixture
files under ``backend/tests/fixtures/plans/`` and as
:mod:`backend.plan_seed` — so it can be reproduced anywhere:

    python scripts/seed_ci_contract_plans.py --plans-dir <dir>

This script is the **command-line face** of that module: it keeps the
command-line contract it has always had and delegates the work, so
there is one implementation of the seed rather than two that drift.

What it does
------------
1. Copies the three tracked files of each seed plan into
   ``<plans-dir>/<plan-id>/``, byte for byte.
2. Writes the two state rows the plan's status is read from, with the
   same values the previous inline seed used: a ``plan_routing`` row in
   the ``verification`` phase and a ``plan_verification`` row that is
   ``pending`` at round 0 of 4.

What it does not do
-------------------
It does not touch any other plan in the target directory, and it does
not overwrite an existing state row — a second run is the same run as
the first.  The seeding is idempotent so a service start can call it
unconditionally.

How a service starts from the tracked copy
-------------------------------------------
The seed belongs to the repository, so the service's plans directory is
seeded by running this script rather than by re-writing the fixtures
inline.  A service start that owns its plans root looks like this::

    export PDT_PLANS_DIR="$(pwd)/<plans-root>"
    export PDT_STATE_DB_PATH="$(pwd)/<state-db>"
    python scripts/seed_ci_contract_plans.py

Both environment variables are honoured by the script's defaults, so
the command line above needs no arguments: the script resolves the same
plans root and the same state database the service does.  The script
exits non-zero when the seed could not be written, so a start that
depends on the two plan cards fails loudly instead of serving an empty
plan list.

Seeding what a service's own declaration names
----------------------------------------------
``--start-cmd`` seeds the plans root a *service's* start command
names — the same target the service manager derives on its own, so an
operator can put the tracked bytes where a service is about to serve
them::

    python scripts/seed_ci_contract_plans.py --start-cmd "$(cat start.sh)"

The command is read as if it ran in the current directory.  The reading
of it — how the pinned environment is read and how a named path is
resolved — is the **project's own** service manager's, and it is handed
to the seed module explicitly: the seed module must not reach for a
service manager on its own, because the copy it would find is the
harness' one, which knows nothing about this project's declarations.

The service start path does not have to run this script at all: the
seed is wired into :mod:`service_manager`, so a service whose own
declaration names the seed plans gets the tracked bytes materialised
into the plans root it serves.  This script stays the way to seed a
plans root by hand, and the way to seed one that no service owns.

Exit codes
----------
0
    The seed is in place.
1
    The seed could not be written (missing fixture, unreadable target,
    unwritable state database).  The reason is printed on stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

#: The repository root — derived from this file's own location so the
#: script can be run from any working directory.
REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: The backend package root.  The script imports the plan seed from
#: there, exactly as the server does.
BACKEND_DIR: Path = REPO_ROOT / "backend"

# The backend package is not installed as a distribution; the modules
# the script needs are imported by their bare name (``plan_seed``,
# ``config_paths``), so the backend root has to be importable.  It
# is *appended* rather than prepended: nothing in ``backend/`` shadows
# a standard-library module, and appending keeps the standard library
# in front either way.
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

# The seed itself.  This import has to come **after** the path setup
# above: on a clean checkout the backend package is not installed, so
# ``plan_seed`` is only importable once its directory is on the path.
from plan_seed import (  # noqa: E402
    SEED_FILE_NAMES,
    SEED_PLAN_IDS,
    EnvReader,
    PathResolver,
    SeedResult,
    copy_seed_files,
    seed_plans,
    seed_targets,
    write_state_rows,
)


def resolve_plans_dir() -> Path:
    """Return the plans root to seed when the caller did not name one."""
    from config_paths import resolve_plans_dir as _resolve

    return _resolve()


def resolve_state_db_path() -> Path:
    """Return the state database to write into when the caller did not name one."""
    from config_paths import resolve_state_db_path as _resolve

    return _resolve()


def readers() -> Tuple[EnvReader, PathResolver]:
    """The readers this script hands to the seed module.

    They are the **project's own** service manager's: the script runs
    the project's backend, and the seed has to read a start command the
    way the service that owns it reads it.

    The seed module does not import a service manager on its own — it
    would find the harness' copy, which knows nothing about this
    project's declarations — so the caller passes the readers in.

    Returns:
        The environment reader and the path resolver.
    """
    import service_manager

    return service_manager.start_cmd_env, service_manager.resolve_declared_data_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the seeding from the command line."""
    parser = argparse.ArgumentParser(
        description="Seed the tracked CI-contract plan fixtures into a plans root.",
    )
    parser.add_argument(
        "--plans-dir",
        default=None,
        help="the plans root to seed (default: PDT_PLANS_DIR, else the repository's plans/)",
    )
    parser.add_argument(
        "--state-db",
        default=None,
        help="the state database to write the plan rows into "
        "(default: PDT_STATE_DB_PATH, else the repository's .pdt/state.db)",
    )
    parser.add_argument(
        "--start-cmd",
        default=None,
        help="seed the plans root this service start command names "
        "(read as if the command ran in the current directory)",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="copy the plan files without writing any state row",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="say nothing on success",
    )
    args = parser.parse_args(argv)

    try:
        plans_dir: Optional[Path] = None
        state_db_path: Optional[Path] = None
        plan_ids = SEED_PLAN_IDS
        write_state = not args.no_state
        if args.start_cmd:
            # What the command names wins: it is the target the
            # service itself serves, and the seed has to land there.
            # The rule is the service manager's, read the same way:
            # a command that names no plans root, no state database or
            # no seed plan is a command that wants no seed.
            env_reader, path_resolver = readers()
            targets = seed_targets(
                args.start_cmd,
                Path.cwd(),
                env_reader=env_reader,
                path_resolver=path_resolver,
            )
            plans_dir = targets.plans_root
            state_db_path = targets.state_db_path
            plan_ids = targets.plan_ids
            write_state = state_db_path is not None
            if plans_dir is None:
                raise ValueError(
                    "the start command names no plans root; nothing to seed"
                )
            if not plan_ids:
                raise ValueError(
                    "the start command names no seed plan; nothing to seed"
                )
        if plans_dir is None:
            plans_dir = Path(args.plans_dir) if args.plans_dir else resolve_plans_dir()
        if state_db_path is None and write_state:
            state_db_path = Path(args.state_db) if args.state_db else resolve_state_db_path()
        result = seed_plans(
            plans_dir,
            state_db_path=state_db_path,
            write_state=write_state,
            plan_ids=plan_ids,
        )
    except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not crash
        print(f"seed_ci_contract_plans: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"seeded {len(result.copied_files)} files into {result.plans_dir}")
        for plan_id in result.plan_ids:
            print(f"  {plan_id}")
        if result.routing_written:
            print(f"routing rows written: {', '.join(result.routing_written)}")
        if result.verification_written:
            print(f"verification rows written: {', '.join(result.verification_written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
