"""The framework-fix lane's shared fixtures — the tests that pin them.

Why this file exists
--------------------
The framework-fix plan puts five shared fixtures in
``backend/tests/conftest.py``: an isolated state database, a committable
fixture repository, four deterministic failure sources, a framework-fix
plan factory and an injectable clock.  They are the substrate the
registry integration tests and the reproduction-protocol tests stand on,
so the fixtures need their own tests: a fixture that silently points at
the operator's live database, or one whose clock runs backwards, makes
every test built on it lie — and it lies *quietly*, which is the kind
of lie a test suite cannot catch on its own.

What is pinned here
-------------------
* ``state_db`` — the file lives inside this test's ``tmp_path``, the
  application itself resolves *that* file rather than the live
  database, and the framework-fix migration has already run against it.
* ``fixture_repo`` — the clone checks out ``main_commit`` and its
  working tree is clean afterwards.
* ``frozen_clock`` — advancing moves the clock forward, and the clock
  never runs backwards.  The fixture replaces the *time source* only:
  ``time.sleep`` is still the real one.
* ``failure_fixtures`` — the four failure sources are executable and
  behave as advertised: deterministic / flake / timeout / green.
* ``framework_fix_plan_factory`` — N plans, each one a framework-fix
  plan with its own fingerprint and at least one attach record.

Two more properties are pinned because they are what makes the
fixtures usable *at all* rather than merely present:

* the fixture repository is built without the developer's git
  configuration — a global ``core.hooksPath`` or ``init.templateDir``
  would otherwise install hooks into the fixture and make every test
  that uses it depend on the machine it runs on;
* the clock is the *shape the scheduler takes as its clock* — a
  zero-argument callable returning epoch seconds — so a threshold can
  be exercised by advancing it instead of by waiting ten minutes.

Negative controls
-----------------
Five of these assertions exist to catch a fixture that is *too* weak
rather than too strong:

* the clock assertion that ``time.sleep`` is untouched — a fixture
  that swallowed sleeps would turn a ten-minute threshold into a
  millisecond one and every timing test would stop meaning anything;
* the ``flake`` source being run twice — a source that fails twice is
  not a flake, it is a deterministic failure wearing the wrong name;
* the ``timeout`` source being given a real deadline — a source that
  merely sleeps is not a timeout;
* the hostile git configuration being shown to really install a hook
  for a plain ``git init`` — without that control the hermeticity
  assertion would pass on a machine where the poison never bit;
* the plan factory being called twice — a factory that stamped the
  wall clock into its plans would make every downstream test depend on
  when it ran.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

#: The backend package root — ``<repo>/backend``.  This module lives in
#: ``backend/tests/unit/``, so ``parents[2]`` is it.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: Captured at import time, before any fixture runs: the real
#: ``time.sleep``.  The clock fixture must leave it alone.
_REAL_SLEEP = time.sleep

#: The other two timers, captured the same way.  The clock fixture
#: replaces the *time source*; the timers are what the process uses to
#: wait, and a fixture that froze them would make every duration read
#: as zero.
_REAL_TIME = time.time
_REAL_MONOTONIC = time.monotonic


@pytest.fixture(autouse=True)
def _hostile_global_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install a global git configuration that would break the fixture repo.

    Autouse on purpose: the point is not that *this* test checks
    hermeticity, it is that **every** test in this module runs with a
    hostile git configuration in place.  A developer's machine can carry

    * ``init.templateDir`` — git copies the template's ``hooks/`` into
      every repository it creates, so a ``pre-commit`` hook from a
      project template lands in the fixture repository; and
    * ``core.hooksPath`` — git then *runs* the hook from there, and a
      hook that fails (a linter that fails on the fixture's two
      throw-away files, a hook that needs the project's node_modules)
      aborts the fixture's commits.

    Either one would make the fixture repository behave differently on
    different machines, which is the opposite of what a test fixture is
    for.  The fixture therefore builds its repository with the global
    and system git configurations neutralised — and this fixture is
    what makes that observable: if the fixture ever stops neutralising
    them, the fixture repository's own commits start failing.

    The poison is installed into both ``GIT_CONFIG_GLOBAL`` and
    ``GIT_CONFIG_SYSTEM`` because the fixture has to neutralise both.
    """
    template = tmp_path / "hostile_template"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    poisoned = tmp_path / "hostile.gitconfig"
    poisoned.write_text(
        "[init]\n"
        f"\ttemplateDir = {template}\n"
        "[core]\n"
        f"\thooksPath = {template / 'hooks'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(poisoned))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(poisoned))


# ---------------------------------------------------------------------------
# state_db
# ---------------------------------------------------------------------------
def test_state_db_isolated(state_db: Path, tmp_path: Path) -> None:
    """The state database is this test's own file, and it is migrated.

    Three separate claims, each of which can fail on its own:

    1. the fixture hands back a path inside ``tmp_path``;
    2. the *application* resolves that same path — the isolation is
       only real if ``config_paths.resolve_state_db_path()`` agrees,
       because that is the function every backend module calls;
    3. the framework-fix migration ran, so the eight framework-fix
       tables are there and a registry test does not have to migrate
       by hand.
    """
    import config_paths

    assert isinstance(state_db, Path), (
        f"state_db must hand back a Path; got {type(state_db).__name__}"
    )

    root = tmp_path.resolve()
    resolved = state_db.resolve()
    assert resolved.is_relative_to(root), (
        f"state_db must live inside tmp_path; {resolved} is outside {root}"
    )

    app_resolved = config_paths.resolve_state_db_path().resolve()
    assert app_resolved.is_relative_to(root), (
        "the application must resolve the state database to the same "
        f"tmp_path file; it resolved {app_resolved}, which is outside {root}"
    )
    assert app_resolved != Path(config_paths.STATE_DB).resolve(), (
        "the application resolved the live state database — the fixture "
        "did not redirect it"
    )

    conn = sqlite3.connect(str(state_db))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()

    missing = {
        "framework_fix_hit",
        "framework_fix_registry",
        "framework_fix_alias",
        "framework_fix_attach",
        "framework_fix_event",
        "framework_fix_merge_lease",
        "framework_fix_repro",
        "plan_verification_result",
    } - tables
    assert not missing, (
        f"the state_db fixture must run the migration; missing tables: "
        f"{sorted(missing)}"
    )


def test_state_db_refuses_to_leave_tmp_path(
    request, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state database outside ``tmp_path`` is refused, loudly.

    The negative control for the isolation rule above: with the
    application's resolver pointed back at the live database, the
    fixture must fail rather than hand out a database that writes into
    the operator's data.  A fixture that fell back silently would look
    exactly like a passing test until the day the operator's plan list
    showed a ``vp-*`` entry that nobody created.
    """
    import config_paths

    monkeypatch.setattr(
        config_paths, "resolve_state_db_path", lambda: config_paths.STATE_DB
    )

    with pytest.raises(RuntimeError):
        request.getfixturevalue("state_db")


# ---------------------------------------------------------------------------
# fixture_repo
# ---------------------------------------------------------------------------
def test_fixture_repo_usable(fixture_repo) -> None:
    """The clone checks ``main_commit`` out and stays clean.

    "Clean" is the load-bearing word: the reproduction protocol clones
    the repository and runs the failing command in the clone, so a
    fixture whose clone starts dirty would make every reproduction
    verdict depend on the fixture rather than on the defect.
    """
    origin = fixture_repo["origin"]
    clone = fixture_repo["clone"]
    main_commit = fixture_repo["main_commit"]

    assert isinstance(origin, Path) and origin.is_dir()
    assert isinstance(clone, Path) and clone.is_dir()
    assert isinstance(main_commit, str) and len(main_commit) == 40

    # The origin really carries the commit.
    origin_head = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert origin_head.stdout.strip() == main_commit, (
        "the origin must carry main_commit; "
        f"got {origin_head.stdout.strip()!r}, expected {main_commit!r}"
    )

    # The clone can check the commit out.
    subprocess.run(
        ["git", "-C", str(clone), "checkout", "--detach", main_commit],
        capture_output=True,
        text=True,
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert head.stdout.strip() == main_commit

    status = subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == "", (
        f"the clone must be clean after checkout; git status said: "
        f"{status.stdout!r}"
    )

    # The clone's origin points at the fixture's origin, so a fetch in
    # the clone reaches the fixture and nothing else.
    remote = subprocess.run(
        ["git", "-C", str(clone), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(remote.stdout.strip()).resolve() == origin.resolve()


def test_fixture_repo_ignores_the_hostile_global_git_config(
    fixture_repo, tmp_path: Path
) -> None:
    """The fixture repository is built without the developer's git config.

    The module's autouse fixture puts a hostile global git
    configuration in place (see
    :func:`_hostile_global_git_config`): a template directory whose
    ``pre-commit`` hook fails, wired in through both ``init.templateDir``
    and ``core.hooksPath``.  A fixture repository that inherited it
    would have its commits aborted by the hook — so the fixture's own
    two commits surviving is already the first half of the proof.

    The second half is explicit: the hook must not be *there*.  A
    repository that carried the hook but happened not to run it would
    still be one commit away from a broken test.

    The negative control comes first, because without it this test
    would also pass on a machine where the poison never bit: a plain
    ``git init`` under the same configuration really does install the
    hook.
    """
    # --- negative control: the poison is real here -------------------
    control = tmp_path / "control_repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(control)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (control / ".git" / "hooks" / "pre-commit").exists(), (
        "the hostile git configuration must really install a hook for a "
        "plain git init; if it does not, the assertions below prove "
        "nothing about the fixture"
    )

    # --- the fixture repository carries no hook ----------------------
    for repo in (tmp_path / "seed", fixture_repo["clone"]):
        assert not (repo / ".git" / "hooks" / "pre-commit").exists(), (
            f"the fixture repository {repo} must not inherit the "
            "developer's git hooks — a test fixture that behaves "
            "differently per machine is not a fixture"
        )


# ---------------------------------------------------------------------------
# frozen_clock
# ---------------------------------------------------------------------------
def test_clock_advance(frozen_clock) -> None:
    """Advancing moves the clock forward, and it never moves back.

    The scheduler's time thresholds (ten minutes to a deadlock, thirty
    minutes to a reminder, two hours to a merge wait) are checked
    against an injected clock rather than against a sleep, so the
    clock's two properties are the whole contract: it moves forward by
    exactly what it was told, and it never goes backwards — a clock
    that can go backwards would let a "the deadline has passed" check
    flip back to "not yet" and the tick would un-expire a plan.
    """
    from framework import clock as framework_clock

    start = frozen_clock.now()
    assert start.tzinfo is not None, "the clock must hand back tz-aware UTC"

    frozen_clock.advance(600)
    later = frozen_clock.now()
    assert later > start, "advance must move the clock forward"
    assert (later - start).total_seconds() == 600, (
        "advance must move the clock by exactly the number of seconds "
        f"asked for; moved {(later - start).total_seconds()}s"
    )

    frozen_clock.advance(60)
    assert (frozen_clock.now() - start).total_seconds() == 660

    # Backwards is refused, and the clock is left where it was.
    before_refusal = frozen_clock.now()
    with pytest.raises(ValueError):
        frozen_clock.advance(-1)
    assert frozen_clock.now() == before_refusal, (
        "a refused advance must leave the clock where it was"
    )
    assert (frozen_clock.now() - start).total_seconds() == 660

    # The backend's single timestamp source follows the injected clock:
    # ``framework.clock.utcnow_iso()`` is where every framework
    # timestamp comes from, so a clock fixture that did not reach it
    # would freeze nothing.
    current = frozen_clock.now()
    assert framework_clock.utcnow_iso() == current.isoformat(
        timespec="microseconds"
    ), (
        "the frozen clock must replace the framework's time source; "
        f"utcnow_iso() said {framework_clock.utcnow_iso()!r} while the "
        f"clock said {current.isoformat(timespec='microseconds')!r}"
    )


def test_clock_does_not_swallow_sleep(frozen_clock) -> None:
    """The clock replaces the time source — it does not fake the waits.

    A fixture that turned ``time.sleep`` into a no-op would make every
    timing test in the suite finish instantly and pass for reasons that
    have nothing to do with the code under test.  The real sleep is
    captured at import time, before any fixture runs, so this compares
    against the truth rather than against the fixture's own view.
    """
    assert time.sleep is _REAL_SLEEP, (
        "the frozen_clock fixture must not replace time.sleep — it "
        "replaces the time source only"
    )


def test_clock_is_a_drop_in_for_the_schedulers_clock(
    frozen_clock, tmp_path: Path
) -> None:
    """The clock is the shape the scheduler takes as its clock.

    The scheduler does not read the wall clock: it takes a clock as a
    parameter -- ``plan_scheduler.capacity.CapacityLedger``,
    ``plan_scheduler.engine.Engine`` and ``plan_scheduler.dryrun`` all
    default to ``time.time`` and accept "a zero-argument callable
    returning epoch seconds" instead.  That is the seam decision 8's
    thresholds are tested through ("inject the clock, advance 600
    seconds"), so the fixture's clock has to *be* that callable -- a
    clock object with only a ``now()`` method would have to be wrapped
    by every caller, and the wrapping is exactly the kind of thing that
    quietly hands the wall clock back.

    The assertions are in the scheduler's own terms: the dry-run
    writer names its artifacts after the day its clock is on, so
    advancing the frozen clock by a day has to roll that name over.
    """
    from plan_scheduler.dryrun import DryRunWriter

    assert callable(frozen_clock), (
        "the frozen clock must be callable: the scheduler takes its "
        "clock as a zero-argument callable returning epoch seconds"
    )
    assert isinstance(frozen_clock(), float), (
        "the scheduler's clock returns epoch seconds as a float"
    )
    assert frozen_clock() == frozen_clock.now().timestamp(), (
        "the callable reading and now() must be the same instant"
    )

    writer = DryRunWriter(tmp_path / "dryrun", clock=frozen_clock)
    assert writer.day_dir.name == "20260101", (
        "the scheduler's day must be the frozen one; it said "
        f"{writer.day_dir.name!r}"
    )

    frozen_clock.advance(86400)
    assert writer.day_dir.name == "20260102", (
        "advancing the clock by a day must roll the scheduler's day "
        f"over; it said {writer.day_dir.name!r}"
    )


def test_clock_does_not_touch_the_timers(frozen_clock) -> None:
    """The clock replaces the clock -- not the timers.

    ``time.sleep`` is already pinned above.  The other two timers get
    the same treatment here: a fixture that froze ``time.time`` would
    make every duration in the process read as zero (pytest's own
    timings included), and a fixture that froze ``time.monotonic``
    would make every timeout in the process fire at once.  Neither
    belongs to "the time source" -- the scheduler's thresholds are
    measured against the injected clock, and the timers are what the
    process uses to *wait*.
    """
    assert time.time is _REAL_TIME, (
        "the frozen_clock fixture must not replace time.time -- it "
        "replaces the time source only"
    )
    assert time.monotonic is _REAL_MONOTONIC, (
        "the frozen_clock fixture must not replace time.monotonic -- it "
        "replaces the time source only"
    )


# ---------------------------------------------------------------------------
# failure_fixtures
# ---------------------------------------------------------------------------
def test_failure_commands(failure_fixtures) -> None:
    """The four failure sources are executable and behave as advertised.

    Each source is a real command, run as a real subprocess: the
    reproduction protocol runs the failing command twice on a clean
    ``main``, so a source that only *looks* like a failure would make
    every classification test a test of the fixture.
    """
    assert set(failure_fixtures) == {
        "deterministic",
        "flake",
        "timeout",
        "green",
    }, (
        "failure_fixtures must provide exactly the four sources the "
        f"reproduction protocol classifies; got {sorted(failure_fixtures)}"
    )

    # --- deterministic: fails the same way, every time ---------------
    deterministic = failure_fixtures["deterministic"]
    for _ in range(2):
        result = subprocess.run(
            deterministic["command"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1, (
            f"the deterministic source must fail; got {result.returncode}"
        )
        assert deterministic["first_error_line"] in result.stderr, (
            "the deterministic source must print its first error line; "
            f"stderr was {result.stderr!r}"
        )

    # --- flake: fails first, passes second ---------------------------
    flake = failure_fixtures["flake"]
    first = subprocess.run(
        flake["command"], capture_output=True, text=True, timeout=60
    )
    assert first.returncode == 1, (
        f"the flake source must fail on its first run; got {first.returncode}"
    )
    assert flake["first_error_line"] in first.stderr
    second = subprocess.run(
        flake["command"], capture_output=True, text=True, timeout=60
    )
    assert second.returncode == 0, (
        "the flake source must pass on its second run; got "
        f"{second.returncode}"
    )

    # --- timeout: does not finish inside the deadline ----------------
    timeout_source = failure_fixtures["timeout"]
    assert timeout_source["timeout_s"] > 0
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            timeout_source["command"],
            capture_output=True,
            text=True,
            timeout=timeout_source["timeout_s"],
        )

    # --- green: passes -----------------------------------------------
    green = failure_fixtures["green"]
    result = subprocess.run(
        green["command"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"the green source must pass; got {result.returncode}"
    )


def test_failure_fixtures_exit_codes_match_what_they_advertise(
    failure_fixtures,
) -> None:
    """The advertised exit codes are the ones the sources produce.

    ``failure_fixtures`` publishes an ``expected_exit_codes`` tuple per
    source, and the reproduction protocol is supposed to compare a
    command's exit code against it.  A fixture that advertised the
    wrong tuple would make every classification test compare against a
    lie — and the failure would show up as a misclassification rather
    than as a broken fixture.

    The ``timeout`` source is not in this list: it never finishes, so
    it has no exit code to advertise — it has a deadline.
    """
    for name in ("deterministic", "flake", "green"):
        source = failure_fixtures[name]
        observed = []
        for _ in range(len(source["expected_exit_codes"])):
            result = subprocess.run(
                source["command"], capture_output=True, text=True, timeout=60
            )
            observed.append(result.returncode)
        assert tuple(observed) == tuple(source["expected_exit_codes"]), (
            f"the {name} source advertised exit codes "
            f"{source['expected_exit_codes']} but produced {tuple(observed)}"
        )


# ---------------------------------------------------------------------------
# framework_fix_plan_factory
# ---------------------------------------------------------------------------
def test_framework_fix_plan_factory(framework_fix_plan_factory) -> None:
    """N framework-fix plans, each with its own fingerprint and attach.

    The factory is what the scheduler and attach tests seed from, so
    the properties that matter are: the count is exactly what was
    asked for, every plan is a framework-fix plan, no two plans share
    a fingerprint (a shared fingerprint is one defect, not two), and
    every plan carries the attach record that says which business plan
    is waiting on it.
    """
    assert framework_fix_plan_factory(0) == []

    plans = framework_fix_plan_factory(3)
    assert len(plans) == 3

    fingerprints = set()
    plan_ids = set()
    for plan in plans:
        assert isinstance(plan, dict)
        assert plan["plan_kind"] == "framework_fix"
        assert plan["plan_id"] not in plan_ids, "plan ids must be unique"
        plan_ids.add(plan["plan_id"])

        fingerprint = plan["fingerprint"]
        assert fingerprint.startswith("sha256:")
        assert fingerprint not in fingerprints, (
            "two plans must not share a fingerprint — that is one defect"
        )
        fingerprints.add(fingerprint)

        # The plan_routing block is what the read model projects.
        block = plan["framework_fix"]
        assert isinstance(block, dict)
        assert block["fingerprint"] == fingerprint

        assert plan["attach"], "every fix plan must carry an attach record"
        for record in plan["attach"]:
            assert record["fingerprint"] == fingerprint
            assert record["status"] == "blocked"
            assert record["plan_id"], "an attach names the waiting plan"


def test_plan_factory_is_deterministic(framework_fix_plan_factory) -> None:
    """Two calls with the same count produce the same plans.

    The factory's timestamps come from the injected clock and its
    fingerprints from the production functions, so the plans it builds
    must be reproducible: a factory that stamped the wall clock into
    them would make every downstream test depend on *when* it ran,
    and a test that failed only after midnight would be unexplainable.
    """
    assert framework_fix_plan_factory(4) == framework_fix_plan_factory(4)