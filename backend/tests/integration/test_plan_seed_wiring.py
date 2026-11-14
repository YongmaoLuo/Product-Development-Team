"""Integration: the tracked CI-contract seed plans reach the service's plans root.

Why this file exists
--------------------
The two CI-contract seed plans — ``ci-contract-broken`` and
``ci-contract-valid`` — are the plan cards the verification-status
endpoint has to show.  Their bytes are tracked in the repository
(``backend/tests/fixtures/plans/``) and a tracked script can copy them
into a plans root, but a script nothing calls is not an entry point: a
clean checkout still had no way to make the two cards appear, because
the only copy that ever reached a running service was the inline
heredoc inside the plan's own ``start_cmd`` — plan *data*, which the
repository does not track.

This file pins the wiring that was missing:

  * the service start path (:func:`service_manager._ensure_services`)
    materialises the **tracked** bytes into the plans root the service's
    own declaration names;
  * it does so only for a declaration that **names** the seed ids — a
    service that does not ask for the seed gets not one byte written
    (the zero-regression promise);
  * the seeding happens **after** the service is ready, so the tracked
    bytes are the ones the service ends up serving;
  * an existing routing / verification row is never overwritten, so a
    restart inside a round does not rebuild the round's state.

And it pins the **load contract** the wiring depends on: the service
manager has to load the way the framework loads it — from its path,
with the harness' ``backend/`` on ``sys.path`` — or the seed is not
merely unused but gone, replaced by the harness' own copy of the module.

The controls are the point.  The positive control is the tracked bytes
arriving byte for byte; the negative control is a declaration that
names no seed id and a plans root that does not move at all.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import PortProbe  # noqa: E402


#: The two seed plans the CI-contract verification point needs: one
#: carries a contract that is not valid JSON, the other one a contract
#: that declares an exit code, a budget and a summary format.
BROKEN_PLAN_ID = "ci-contract-broken"
VALID_PLAN_ID = "ci-contract-valid"
SEED_PLAN_IDS = (BROKEN_PLAN_ID, VALID_PLAN_ID)

#: The three files every seeded plan carries.
SEED_FILE_NAMES = ("ci_contract.json", "verification_plan.json", "interview.json")

#: The tracked fixtures the seed is copied from.
FIXTURE_ROOT = _BACKEND_DIR / "tests" / "fixtures" / "plans"

#: A port nothing in this repository reserves.
_PORT = 45871


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_cmd(
    plans_root_name: str, state_db_name: str, *, seed_ids=SEED_PLAN_IDS,
) -> str:
    """A start command in the shape the CI-contract plan's own uses.

    It pins the two environment variables the service's data plane is
    named by — the plans root and the state database — and it names the
    seed ids it wants, the way the real declaration's inline seed does.
    The paths are spelled ``$(pwd)/…`` because that is what the real
    declaration spells, and because it is the case that has to be
    resolved against the declaration's working directory.
    """
    lines = [
        "set -eu",
        f'export PDT_PLANS_DIR="$(pwd)/{plans_root_name}"',
        f'export PDT_STATE_DB_PATH="$(pwd)/{state_db_name}"',
        "exec python - <<'PY'",
        "import os",
        "from pathlib import Path",
        "PLANS_DIR = Path(os.environ['PDT_PLANS_DIR'])",
    ]
    for plan_id in seed_ids:
        lines.append(f"SEED_PLAN_ID = '{plan_id}'")
    lines.append("PY")
    return "\n".join(lines) + "\n"


def _declaration(
    plans_root_name: str = "plans",
    state_db_name: str = "state.db",
    *,
    seed_ids=SEED_PLAN_IDS,
) -> ServiceDeclaration:
    """The declaration of a service that serves the CI-contract plans."""
    return ServiceDeclaration(
        name="api",
        port=_PORT,
        start_cmd=_start_cmd(plans_root_name, state_db_name, seed_ids=seed_ids),
        cwd=".",
    )


def _stub_the_service(monkeypatch) -> None:
    """Let ``_ensure_services`` reach the seeding step without a server.

    The seeding happens **after** the service is ready, so the case has
    to get past the start.  What the start does is not what is under
    test here: a real ``backend.server`` would bind a port and outlive
    the case.  The port probe is stubbed to "nothing is listening", the
    start to "the service came up", and the data-plane probe to the
    "absent" verdict — the one a project service without a database of
    its own gets, and the one the data-plane gate never refuses.
    """
    monkeypatch.setattr(
        sm, "probe_port",
        lambda port: PortProbe(port=port, listening=False),
    )

    def _fake_start(decl, project_dir, **kwargs):
        return sm.ServiceRuntime(
            name=decl.name, port=decl.port, start_cmd=decl.start_cmd,
            cwd=decl.cwd, origin="spawned", ready=True,
        )

    monkeypatch.setattr(sm, "start_service", _fake_start)
    monkeypatch.setattr(
        sm, "data_plane_evidence",
        lambda port, **kwargs: sm.DataPlaneEvidence(verdict=sm.DATA_PLANE_ABSENT),
    )


def _ensure(project_dir: Path, decl: ServiceDeclaration) -> sm.ServiceRuntimeMap:
    """Run the real ensure path over one declaration."""
    plan_dir = project_dir / "plan-dir"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return sm._ensure_services(
        project_dir, {decl.name: decl}, plan_dir=plan_dir,
    )


def _snapshot(root: Path) -> dict:
    """Every file under *root*, by relative path, as bytes."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _read_rows(state_db_path: Path, plan_id: str) -> tuple:
    """The routing row and the verification row of one plan."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import VerificationRepository

    conn = open_db(state_db_path)
    try:
        routing = RoutingRepository(conn)
        verification = VerificationRepository(conn)
        return routing.find(plan_id), verification.current(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The wiring
# ---------------------------------------------------------------------------


def test_the_ensure_path_seeds_the_tracked_bytes_into_the_plans_root(
    tmp_path: Path, monkeypatch,
) -> None:
    """The tracked copy reaches the plans root through the start path.

    This is the control that says the seed is *wired*: the bytes arrive
    because the service was ensured, not because the test called a
    helper.  Byte for byte — a re-serialised JSON is a lookalike, not
    the tracked fixture.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_the_service(monkeypatch)

    result = _ensure(project_dir, _declaration())

    assert result.runtimes["api"].ready is True
    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            seeded = project_dir / "plans" / plan_id / name
            assert seeded.is_file(), f"{plan_id}/{name} was not seeded"
            assert seeded.read_bytes() == (FIXTURE_ROOT / plan_id / name).read_bytes(), (
                f"{plan_id}/{name} is not the tracked fixture"
            )


def test_a_service_naming_no_seed_id_is_left_byte_for_byte_alone(
    tmp_path: Path, monkeypatch,
) -> None:
    """The negative control: no seed named, not one byte written.

    This is the zero-regression promise of the PRD — a service that
    does not ask for the CI-contract seed must behave exactly as it
    did before the wiring existed.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plans_root = project_dir / "plans"
    (plans_root / "some-other-plan").mkdir(parents=True)
    (plans_root / "some-other-plan" / "tasks.json").write_text("[]", encoding="utf-8")
    before = _snapshot(plans_root)
    _stub_the_service(monkeypatch)

    _ensure(project_dir, _declaration(seed_ids=()))

    assert _snapshot(plans_root) == before
    assert not (plans_root / BROKEN_PLAN_ID).exists()
    assert not (plans_root / VALID_PLAN_ID).exists()


def test_a_plan_that_is_not_a_seed_is_not_touched(
    tmp_path: Path, monkeypatch,
) -> None:
    """Seeding one plan does not disturb the plans around it."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plans_root = project_dir / "plans"
    (plans_root / "some-other-plan").mkdir(parents=True)
    (plans_root / "some-other-plan" / "tasks.json").write_text("[]", encoding="utf-8")
    (plans_root / "some-other-plan" / "interview.json").write_text(
        '{"dimensions": {"goals": "not a seed"}}', encoding="utf-8",
    )
    _stub_the_service(monkeypatch)

    _ensure(project_dir, _declaration())

    assert (plans_root / "some-other-plan" / "tasks.json").read_text(
        encoding="utf-8",
    ) == "[]"
    assert (plans_root / "some-other-plan" / "interview.json").read_text(
        encoding="utf-8",
    ) == '{"dimensions": {"goals": "not a seed"}}'


def test_seeding_through_the_ensure_path_twice_changes_nothing(
    tmp_path: Path, monkeypatch,
) -> None:
    """The wiring is idempotent — a second ensure is the same ensure."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_the_service(monkeypatch)

    _ensure(project_dir, _declaration())
    before = _snapshot(project_dir / "plans")

    _ensure(project_dir, _declaration())

    assert _snapshot(project_dir / "plans") == before


def test_an_existing_state_row_is_not_overwritten(
    tmp_path: Path, monkeypatch,
) -> None:
    """A row the round has already advanced stays exactly as it is.

    This is what makes a restart inside a round safe: the seed writes
    the rows a *fresh* plan needs and leaves the rows of a plan that is
    already running alone.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    state_db = project_dir / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import VerificationRepository

    conn = open_db(state_db)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(BROKEN_PLAN_ID, "planning")
        VerificationRepository(conn).insert(
            BROKEN_PLAN_ID, "running", round=2, max_rounds=7,
        )
    finally:
        conn.close()
    _stub_the_service(monkeypatch)

    _ensure(project_dir, _declaration())

    routing_row, verification_row = _read_rows(state_db, BROKEN_PLAN_ID)
    assert routing_row is not None
    assert routing_row["current_phase"] == "planning"
    assert verification_row is not None
    assert verification_row["verification_status"] == "running"
    assert verification_row["round"] == 2
    assert verification_row["max_rounds"] == 7


def test_the_seed_writes_the_rows_the_status_endpoint_reads(
    tmp_path: Path, monkeypatch,
) -> None:
    """A fresh plan gets the rows the verification-status endpoint reads."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _stub_the_service(monkeypatch)

    _ensure(project_dir, _declaration())

    state_db = project_dir / "state.db"
    for plan_id in SEED_PLAN_IDS:
        routing_row, verification_row = _read_rows(state_db, plan_id)
        assert routing_row is not None, f"{plan_id} has no routing row"
        assert routing_row["current_phase"] == "verification"
        assert verification_row is not None, f"{plan_id} has no verification row"
        assert verification_row["verification_status"] == "pending"
        assert verification_row["round"] == 0
        assert verification_row["max_rounds"] == 4


# ---------------------------------------------------------------------------
# The load contract
# ---------------------------------------------------------------------------
#
# The framework loads a project's ``backend/service_manager.py`` **from
# its path** (see ``verification_agent._load_service_manager``), with the
# harness' ``backend/`` on ``sys.path`` and the project's own
# ``backend/`` — where ``plan_seed.py`` lives — *not* on it.  A flat
# ``import plan_seed`` therefore resolved to nothing: the whole module
# failed to load, the framework turned that into one warning line and a
# fallback to its own copy, and the seed was wired in and never ran.
#
# The cases below load the module the way the framework loads it, so
# that contract cannot quietly go away again.


def _framework_backend(tmp_path: Path) -> Path:
    """A stand-in for the harness' ``backend/``: every module but the seed.

    The harness' backend holds the modules a project's service manager
    imports flat — and not the project's own ``plan_seed.py``, which
    exists only in the project.  This directory has that shape: every
    entry of this backend, except the seed.

    A symlink is enough on the platforms the suite runs on; where
    symlinks are not available the entry is copied instead.  Either way
    the seed is the one thing the directory does not have.
    """
    import shutil

    framework_backend = tmp_path / "framework-backend"
    framework_backend.mkdir()
    for entry in sorted(_BACKEND_DIR.iterdir()):
        if entry.name in {"plan_seed.py", "__pycache__", ".venv"}:
            continue
        target = framework_backend / entry.name
        try:
            target.symlink_to(entry)
        except OSError:  # pragma: no cover - a platform without symlinks
            if entry.is_dir():
                shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(entry, target)
    return framework_backend


def _harness_service_manager() -> ModuleType:
    """The harness' ``service_manager`` — the copy the framework keeps.

    It carries the two readers **both** trees have (``start_cmd_env``
    and ``_resolve_data_path``) and not the one this branch added
    (``resolve_declared_data_path``): a seed that reached for the
    latter would raise an ``ImportError`` under the framework and write
    nothing at all.

    This stand-in is what ``sys.modules["service_manager"]`` holds
    while the framework loads a project's module — the copy every flat
    import resolves to.
    """
    stand_in = ModuleType("service_manager")
    stand_in.start_cmd_env = sm.start_cmd_env
    stand_in._resolve_data_path = sm._resolve_data_path
    return stand_in


def _load_as_the_framework_loads(
    module_path: Path, framework_backend: Path, monkeypatch,
) -> ModuleType:
    """Load ``module_path`` the way the framework loads a project's module.

    The seed module is not reachable by name while it loads: the harness
    has no copy of it, and neither has ``sys.modules``.  Everything the
    module imports flat still resolves — through the stand-in backend,
    which is what the framework offers it.  The seed is the one thing
    missing, exactly as it is in the framework.
    """
    monkeypatch.delitem(sys.modules, "plan_seed", raising=False)
    monkeypatch.setitem(sys.modules, "service_manager", _harness_service_manager())
    monkeypatch.setattr(sys, "path", [
        str(framework_backend),
        *(
            entry for entry in sys.path
            if not (Path(entry or ".") / "plan_seed.py").is_file()
        ),
    ])

    spec = importlib.util.spec_from_file_location(
        "pdt_project_service_manager_under_test", str(module_path),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_the_service_manager_loads_the_way_the_framework_loads_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """The module has to load under the framework's loader.

    A module that cannot be loaded is a module the framework *replaces*
    with its own copy: the seed wiring is then not merely unused, it is
    gone — and the only trace of it is one warning line.
    """
    framework_backend = _framework_backend(tmp_path)

    module = _load_as_the_framework_loads(
        _BACKEND_DIR / "service_manager.py", framework_backend, monkeypatch,
    )

    assert hasattr(module, "_seed_service_plans"), (
        "the loaded module offers no seeding entry point"
    )


def test_the_framework_loaded_module_seeds_the_tracked_bytes(
    tmp_path: Path, monkeypatch,
) -> None:
    """The seed lands through the module the framework actually loads.

    This is the end of the chain: the framework loads the module, the
    module seeds the plans root, and the tracked bytes are there.
    """
    framework_backend = _framework_backend(tmp_path)
    module = _load_as_the_framework_loads(
        _BACKEND_DIR / "service_manager.py", framework_backend, monkeypatch,
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    module._seed_service_plans(_declaration(), project_dir)

    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            seeded = project_dir / "plans" / plan_id / name
            assert seeded.is_file(), f"{plan_id}/{name} was not seeded"
            assert seeded.read_bytes() == (FIXTURE_ROOT / plan_id / name).read_bytes(), (
                f"{plan_id}/{name} is not the tracked fixture"
            )

    for plan_id in SEED_PLAN_IDS:
        routing_row, verification_row = _read_rows(project_dir / "state.db", plan_id)
        assert routing_row is not None, f"{plan_id} has no routing row"
        assert routing_row["current_phase"] == "verification"
        assert verification_row is not None, f"{plan_id} has no verification row"
        assert verification_row["verification_status"] == "pending"
        assert verification_row["round"] == 0
        assert verification_row["max_rounds"] == 4


# ---------------------------------------------------------------------------
# The tracked source
# ---------------------------------------------------------------------------


def test_the_seed_is_copied_from_files_git_tracks() -> None:
    """The bytes come from the repository, not from a scratch directory.

    A clean checkout has to be able to reproduce the seed; a file the
    repository ignores cannot be reproduced.
    """
    repo_root = _BACKEND_DIR.parent
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout — nothing to ask git about")
    expected = sorted(
        f"backend/tests/fixtures/plans/{plan_id}/{name}"
        for plan_id in SEED_PLAN_IDS
        for name in SEED_FILE_NAMES
    )
    completed = subprocess.run(
        ["git", "ls-files", "--", *expected],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )

    assert sorted(completed.stdout.split()) == expected


def test_the_seed_module_owns_the_registry() -> None:
    """The registry lives in one module, inside the package."""
    import plan_seed

    assert plan_seed.SEED_PLAN_IDS == SEED_PLAN_IDS
    assert plan_seed.SEED_FILE_NAMES == SEED_FILE_NAMES
    assert plan_seed.FIXTURE_ROOT == FIXTURE_ROOT


def test_the_seed_ids_are_read_off_the_declaration() -> None:
    """A declaration names the seeds it wants — and only those."""
    import plan_seed

    named = plan_seed.seed_plan_ids_named_in(
        _start_cmd("plans", "state.db", seed_ids=SEED_PLAN_IDS),
    )
    assert named == SEED_PLAN_IDS

    assert plan_seed.seed_plan_ids_named_in(
        "exec python -m backend.server",
    ) == ()

    # A longer identifier that merely contains a seed id is not a naming.
    assert plan_seed.seed_plan_ids_named_in(
        f"SEED = 'x{BROKEN_PLAN_ID}'",
    ) == ()
