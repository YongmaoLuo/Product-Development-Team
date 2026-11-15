"""Unit tests for the tracked CI-contract seed plans and the seeding script.

Why this file exists
--------------------
The two CI-contract seed plans — ``ci-contract-broken`` and
``ci-contract-valid`` — used to exist only inside an inline Python
heredoc in a verification plan's ``start_cmd``: the plan *data* wrote
``ci_contract.json`` / ``verification_plan.json`` / ``interview.json``
at service start.  Nothing in the repository held a copy, so a clean
checkout could not reproduce the seed, and the e2e verification point
that opens the two plan cards only passed on a machine where the
ignored scratch directory happened to survive.

The seed is now a tracked fixture pair plus a tracked, re-runnable
seeding script.  These tests pin the three properties that make the
difference:

  * the fixtures are exactly what the contract reader has to cope with —
    one deliberately broken contract, one valid one — and the seeding
    copies them byte for byte;
  * the seeding writes the state rows the verification-status endpoint
    reads (``verification`` phase, ``pending``, round 0 of 4);
  * seeding twice changes nothing, and never touches a plan that is not
    one of the seeds.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import ci_contract  # noqa: E402

#: The tracked seed script.
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "seed_ci_contract_plans.py"

#: The tracked fixture root the seed is copied from.
_FIXTURE_ROOT = _BACKEND_DIR / "tests" / "fixtures" / "plans"

BROKEN_PLAN_ID = "ci-contract-broken"
VALID_PLAN_ID = "ci-contract-valid"
SEED_PLAN_IDS = (BROKEN_PLAN_ID, VALID_PLAN_ID)
SEED_FILE_NAMES = ("ci_contract.json", "verification_plan.json", "interview.json")

#: The title the seeded verification plan carries; the verification-status
#: endpoint is asked for exactly this string.
SEEDED_VP_TITLE = "契约计划种子验证点"


def _load_script() -> ModuleType:
    """Import ``scripts/seed_ci_contract_plans.py`` by path.

    The script is a command-line entry point, not a package member, so
    it is loaded the way a reader would run it — from its file — rather
    than by a name that only exists on ``sys.path``.
    """
    spec = importlib.util.spec_from_file_location("seed_ci_contract_plans", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    # The module is registered before it is executed: ``dataclasses``
    # looks the defining module up in ``sys.modules`` while it builds
    # the class, and a module that was never registered is not there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_rows(state_db_path: Path) -> tuple[dict, dict]:
    """Return the routing row and the verification row of every seed plan."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import VerificationRepository

    conn = open_db(state_db_path)
    try:
        routing = RoutingRepository(conn)
        verification = VerificationRepository(conn)
        routing_rows = {plan_id: routing.find(plan_id) for plan_id in SEED_PLAN_IDS}
        verification_rows = {plan_id: verification.current(plan_id) for plan_id in SEED_PLAN_IDS}
    finally:
        conn.close()
    return routing_rows, verification_rows


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------


def test_the_broken_contract_fixture_is_verbatim_broken_json() -> None:
    """The broken seed's contract is bad JSON on purpose — that is the point.

    A fixture whose contract happened to parse would stop exercising the
    "bad contract falls back to the framework defaults" path, and the
    seed would quietly become a second copy of the valid seed.
    """
    raw = (_FIXTURE_ROOT / BROKEN_PLAN_ID / "ci_contract.json").read_text(encoding="utf-8")

    assert raw == "{ this is not json"
    with pytest.raises(ValueError):
        json.loads(raw)


def test_the_valid_contract_fixture_declares_the_documented_fields() -> None:
    """The valid seed's contract declares the four fields the reader reads."""
    raw = (_FIXTURE_ROOT / VALID_PLAN_ID / "ci_contract.json").read_text(encoding="utf-8")

    assert json.loads(raw) == {
        "inconclusive_exit_codes": [7],
        "budget_source": "contract",
        "budget_seconds": 900,
        "summary_format": "gate_v1",
    }


@pytest.mark.parametrize("plan_id", SEED_PLAN_IDS)
def test_each_seed_fixture_carries_all_three_files(plan_id: str) -> None:
    """Every seed is a plan directory with the three files a plan needs."""
    plan_dir = _FIXTURE_ROOT / plan_id

    assert plan_dir.is_dir(), f"{plan_id} is missing from the tracked fixtures"
    for name in SEED_FILE_NAMES:
        assert (plan_dir / name).is_file(), f"{plan_id}/{name} is missing"


@pytest.mark.parametrize("plan_id", SEED_PLAN_IDS)
def test_each_verification_plan_fixture_has_at_least_one_verification_point(plan_id: str) -> None:
    """A seeded plan with no verification point has nothing for the panel to show."""
    raw = (_FIXTURE_ROOT / plan_id / "verification_plan.json").read_text(encoding="utf-8")
    points = json.loads(raw)["verification_points"]

    assert len(points) >= 1
    assert points[0]["title"] == SEEDED_VP_TITLE


@pytest.mark.parametrize("plan_id", SEED_PLAN_IDS)
def test_each_interview_fixture_carries_the_goals_dimension(plan_id: str) -> None:
    """The interview file keeps its goals dimension, so the plan is a plan."""
    raw = (_FIXTURE_ROOT / plan_id / "interview.json").read_text(encoding="utf-8")
    dimensions = json.loads(raw)["dimensions"]

    assert "goals" in dimensions
    assert dimensions["goals"]


def test_the_six_fixture_files_are_tracked_by_git() -> None:
    """The fixtures have to be *tracked* — a file git ignores cannot be reproduced.

    This is the difference the repair was about: the seed used to live
    only in a scratch directory the repository ignores, so a clean
    checkout had nothing to copy.
    """
    if not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout — nothing to ask git about")

    expected = sorted(
        f"backend/tests/fixtures/plans/{plan_id}/{name}"
        for plan_id in SEED_PLAN_IDS
        for name in SEED_FILE_NAMES
    )
    completed = subprocess.run(
        ["git", "ls-files", "--", *expected],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert sorted(completed.stdout.split()) == expected


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seeding_writes_the_three_files_of_both_plans(tmp_path: Path) -> None:
    """The seeded plans carry the same file names as the tracked fixtures."""
    script = _load_script()
    plans_dir = tmp_path / "plans"

    script.seed_plans(plans_dir, state_db_path=tmp_path / "state.db")

    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            assert (plans_dir / plan_id / name).is_file()


def test_seeded_files_are_byte_identical_to_the_tracked_fixtures(tmp_path: Path) -> None:
    """The copy is byte for byte — a re-serialised JSON would not be the same seed."""
    script = _load_script()
    plans_dir = tmp_path / "plans"

    script.seed_plans(plans_dir, state_db_path=tmp_path / "state.db")

    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            assert (plans_dir / plan_id / name).read_bytes() == (
                _FIXTURE_ROOT / plan_id / name
            ).read_bytes()


def test_the_seeded_broken_contract_falls_back_to_the_framework_defaults(tmp_path: Path) -> None:
    """Seeding the broken seed reproduces the fallback the runner depends on."""
    script = _load_script()
    plans_dir = tmp_path / "plans"

    script.seed_plans(plans_dir, state_db_path=tmp_path / "state.db")
    contract = ci_contract.load_ci_contract(plans_dir / BROKEN_PLAN_ID)

    assert contract.applied is False
    assert contract.inconclusive_exit_codes == ci_contract.FRAMEWORK_INCONCLUSIVE_EXIT_CODES
    assert contract.error


def test_the_seeded_valid_contract_is_applied(tmp_path: Path) -> None:
    """Seeding the valid seed reproduces a contract the reader applies."""
    script = _load_script()
    plans_dir = tmp_path / "plans"

    script.seed_plans(plans_dir, state_db_path=tmp_path / "state.db")
    contract = ci_contract.load_ci_contract(plans_dir / VALID_PLAN_ID)

    assert contract.applied is True
    assert contract.inconclusive_exit_codes == (7,)
    assert contract.budget_source == ci_contract.BUDGET_SOURCE_CONTRACT
    assert contract.budget_seconds == 900
    assert contract.summary_format == ci_contract.SUMMARY_FORMAT_GATE_V1


def test_seeding_writes_the_routing_and_verification_rows(tmp_path: Path) -> None:
    """The state rows the status endpoint reads are written by the seed."""
    script = _load_script()
    plans_dir = tmp_path / "plans"
    state_db_path = tmp_path / "state.db"

    script.seed_plans(plans_dir, state_db_path=state_db_path)
    routing_rows, verification_rows = _read_rows(state_db_path)

    for plan_id in SEED_PLAN_IDS:
        routing = routing_rows[plan_id]
        assert routing is not None, f"{plan_id} has no routing row"
        assert routing["current_phase"] == "verification"

        verification = verification_rows[plan_id]
        assert verification is not None, f"{plan_id} has no verification row"
        assert verification["verification_status"] == "pending"
        assert verification["round"] == 0
        assert verification["max_rounds"] == 4


def test_seeding_twice_changes_nothing(tmp_path: Path) -> None:
    """The seed is idempotent: a second run is the same run."""
    script = _load_script()
    plans_dir = tmp_path / "plans"
    state_db_path = tmp_path / "state.db"

    script.seed_plans(plans_dir, state_db_path=state_db_path)
    before = {
        path.relative_to(plans_dir): path.read_bytes()
        for path in sorted(plans_dir.rglob("*"))
        if path.is_file()
    }

    script.seed_plans(plans_dir, state_db_path=state_db_path)
    after = {
        path.relative_to(plans_dir): path.read_bytes()
        for path in sorted(plans_dir.rglob("*"))
        if path.is_file()
    }

    assert after == before
    routing_rows, verification_rows = _read_rows(state_db_path)
    for plan_id in SEED_PLAN_IDS:
        assert routing_rows[plan_id] is not None
        assert verification_rows[plan_id] is not None


def test_seeding_leaves_other_plans_untouched(tmp_path: Path) -> None:
    """The seed writes its own plans and nothing else in the plans tree."""
    script = _load_script()
    plans_dir = tmp_path / "plans"
    other = plans_dir / "some-other-plan"
    other.mkdir(parents=True)
    (other / "tasks.json").write_text("[]", encoding="utf-8")

    script.seed_plans(plans_dir, state_db_path=tmp_path / "state.db")

    assert (other / "tasks.json").read_text(encoding="utf-8") == "[]"


def test_the_script_runs_as_a_command_line_entry_point(tmp_path: Path) -> None:
    """The seeding path is re-runnable from a shell, not only from Python."""
    plans_dir = tmp_path / "plans"
    state_db_path = tmp_path / "state.db"

    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--plans-dir",
            str(plans_dir),
            "--state-db",
            str(state_db_path),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            assert (plans_dir / plan_id / name).is_file()


# ---------------------------------------------------------------------------
# Seeding what a service's own declaration names
# ---------------------------------------------------------------------------
#
# The seed module does not import a service manager: the copy it would
# find is the harness' one, which knows nothing about this project's
# declarations — and a symbol that exists only in this tree (``
# resolve_declared_data_path``) is an outright ``ImportError`` there.
# The readers are handed in by the caller instead, and the script is
# such a caller: it runs the project's backend, so it reads a start
# command the way the service that owns it reads it.


def _start_cmd(plans_root_name: str, state_db_name: str, *, seed_ids=SEED_PLAN_IDS) -> str:
    """A start command in the shape a service declaration's uses."""
    lines = [
        "set -eu",
        f'export PDT_PLANS_DIR="$(pwd)/{plans_root_name}"',
        f'export PDT_STATE_DB_PATH="$(pwd)/{state_db_name}"',
    ]
    for plan_id in seed_ids:
        lines.append(f"SEED_PLAN_ID = '{plan_id}'")
    return "\n".join(lines)


def test_the_script_seeds_the_plans_root_a_start_command_names(tmp_path: Path) -> None:
    """``--start-cmd`` seeds what the command names — the service's own target."""
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--start-cmd",
            _start_cmd("plans", "state.db"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    plans_dir = tmp_path / "plans"
    for plan_id in SEED_PLAN_IDS:
        for name in SEED_FILE_NAMES:
            assert (plans_dir / plan_id / name).is_file()
    routing_rows, verification_rows = _read_rows(tmp_path / "state.db")
    for plan_id in SEED_PLAN_IDS:
        assert routing_rows[plan_id] is not None
        assert verification_rows[plan_id] is not None


def test_a_start_command_that_names_no_seed_writes_nothing(tmp_path: Path) -> None:
    """The negative control: a command that wants no seed gets no seed."""
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--start-cmd",
            _start_cmd("plans", "state.db", seed_ids=()),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "names no seed plan" in completed.stderr
    assert not (tmp_path / "plans").exists()


def test_the_script_hands_its_own_readers_to_the_seed() -> None:
    """The readers are the project's own service manager's.

    A seed module that reached for a service manager on its own would
    find the harness' copy — the one the framework puts on
    ``sys.path`` — and would read the command the wrong way.
    """
    script = _load_script()

    env_reader, path_resolver = script.readers()

    import service_manager

    assert env_reader is service_manager.start_cmd_env
    assert path_resolver is service_manager.resolve_declared_data_path