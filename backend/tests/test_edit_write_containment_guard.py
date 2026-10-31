"""Tests for the sub-agent Edit/Write containment guard (2026-08-26 fix).

Context
-------
The previous guard blocked writes to the checkout the backend runs from *only when the
sub-agent's project_dir was outside it*. Verification self-heal
sub-agents used to edit whatever ``project_dir`` pointed at — when that
was the production checkout (production code), the sub-agent
rewrote 17 production files mid-verification (VP-023 incident). The new
guard is a *containment* rule: a sub-agent may only Edit/Write inside
its own project_dir, the shared plans/ dir, or temp scratch space.

These tests exercise the guard as a black-box: import the production
guard function (which writes the script to a tmpfile once and caches
the path) and run it as a subprocess with PreToolUse-shaped stdin.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from _pytest_hermetic_env import REAL_HOME_ENV
from coding_tool import _build_guard_hook


@pytest.fixture(scope="module")
def guard_path():
    """Force the production guard to materialise its tmpfile."""
    return Path(_build_guard_hook()["command"].split(" ", 1)[1])


def _real_home() -> Path:
    """The home this test run was *launched* with -- not the redirected one.

    Every path this module hands the guard has to be **outside tempdir**, or
    the test stops testing the containment rule: the guard allows temp scratch
    unconditionally (``in_tmp_scratch`` in ``coding_tool.py``), so a target
    under the temp root is allowed no matter which checkout it is "in", and an
    assertion of ``rc == 2`` fails while proving nothing about containment.

    ``Path.home()`` is unusable here. ``install_hermetic_env()`` redirects
    ``$HOME`` at ``tempfile.mkdtemp(prefix="home-")``, which lands *under* the
    temp root -- so ``Path.home()`` returns a path the guard already allows
    and every case below stops discriminating. That is not hypothetical: it
    is what turned these five tests red, and it is silent, because a
    containment test that allows everything still returns a well-formed exit
    code.

    Falls back to nothing: the variable is installed by the same function that
    installs the redirect, so its absence means the environment contract is
    broken, and defaulting to ``Path.home()`` here would quietly restore the
    vacuous version rather than say so.
    """
    value = os.environ.get(REAL_HOME_ENV)
    if not value:
        raise RuntimeError(
            f"{REAL_HOME_ENV} is unset; install_hermetic_env() must run before "
            f"this module derives a path. Falling back to Path.home() would "
            f"return the redirected tempdir home, which the guard allows "
            f"unconditionally -- the cases below would then pass or fail "
            f"without exercising containment at all."
        )
    return Path(value)


# Fixture paths for "another repo" — the containment guard's whole
# purpose is to block edits across repos, so we need concrete paths to
# other checkouts. Resolve from the *launch* home (``_real_home()``, not
# ``Path.home()``) so the test is portable across developer machines and so
# the paths stay outside tempdir -- see ``_real_home`` for why the second
# half is load-bearing. If those checkouts don't exist locally
# the relevant test will simply not find its target.
_FOREIGN_CHECKOUT_DIR = _real_home() / "work" / "other-repo"
_FOREIGN_PRODUCTION_DIR = _real_home() / "work" / "production-checkout"

#: How long one guard invocation may take before the test gives up.
#:
#: The ceiling is there to catch a **hung** guard, not to prove the
#: machine is fast.  It used to be 5 s, which was calibrated on an idle
#: box: the guard itself measures 0.3-1.8 s when it runs alone, but the
#: whole-repo gate runs 18 xdist workers at once, and 18 concurrent
#: invocations measured 9.1-16.2 s (three runs).  A 5 s ceiling therefore
#: failed on the gate's own parallelism — a red that says nothing about
#: the containment rule.  60 s still catches a hang (the guard is a
#: subprocess, so a hang blocks forever) and survives the load.
_GUARD_TIMEOUT_SECONDS = 60.0


def test_the_guard_timeout_is_sized_for_the_gate_own_parallelism():
    """The ceiling must outlast the gate's own concurrency.

    This is the shape of the failure the 5 s ceiling produced: the guard
    was correct, the machine was busy, and the case went red anyway.
    """
    assert _GUARD_TIMEOUT_SECONDS >= 60.0


# ----- the wall-clock budget the guard is given ----------------------------
#
# The guard answers in milliseconds once it is running, but it is started
# with ``python3``, and a cold interpreter on a host that is already busy
# takes a while to say anything at all.  A fixed five-second budget
# therefore measures the *host* rather than the guard, and the failure it
# produces is a ``TimeoutExpired`` -- which says nothing about the
# containment rule these tests exist to pin.

#: The budget a quiet host is given.
GUARD_TIMEOUT_SECONDS = 5.0

#: How many seconds of budget one unit of load average buys.
GUARD_TIMEOUT_LOAD_SECONDS = 1.0

#: The ceiling: a scaled budget is still a budget, so that a genuinely
#: hung guard fails the test instead of hanging it.
GUARD_TIMEOUT_MAX_SECONDS = 60.0


def _guard_timeout_seconds() -> float:
    """The wall-clock budget for one guard invocation, in seconds.

    The budget follows the host's load average: the busier the machine,
    the longer the child may reasonably take to start and answer.  It is
    capped, so a guard that never answers is still a failing test.

    A platform that cannot report a load average gets the base budget --
    the same one the fixed timeout used to be.
    """
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):  # pragma: no cover - exotic platform
        load = 0.0
    budget = GUARD_TIMEOUT_SECONDS + GUARD_TIMEOUT_LOAD_SECONDS * max(0.0, load)
    return min(budget, GUARD_TIMEOUT_MAX_SECONDS)


def _run_guard(file_path: str, *, pdt_project_dir: str, pdt_formal_repo: str = "",
               pdt_plans_dir: str = "", guard: Path = None) -> int:
    """Invoke the guard hook with the same env our sub-agent sees.

    Returns the exit code: 0 = allowed, 2 = blocked. Raises on shell error.

    Note: when the caller passes ``""`` for any of the AC_* slots we
    explicitly ``pop`` the inherited env var. The backend sub-agent harness
    leaks ``PDT_PROJECT_DIR`` / ``PDT_FORMAL_REPO_PATH`` / ``PDT_PLANS_DIR``
    into this test process; without the explicit pop, the legacy-fallback
    test (``test_falls_back_to_formal_only_block_when_project_dir_unknown``)
    inherits a stale ``PDT_PROJECT_DIR`` and the containment branch fires
    instead of the legacy branch (the test asserts rc=2 for a formal-ac
    write but rc=0 leaks back because the containment rule allows
    writes inside /tmp).
    """
    if guard is None:
        guard = Path(_build_guard_hook()["command"].split(" ", 1)[1])
    env = os.environ.copy()
    # Explicitly delete so a stale parent env value never leaks into the
    # guard. ``os.environ.copy()`` carries AC_* from the test runner
    # (sub-agent harness) and would otherwise steer the guard into the
    # containment branch even when the caller asks for the legacy branch.
    env.pop("PDT_PROJECT_DIR", None)
    env.pop("PDT_FORMAL_REPO_PATH", None)
    env.pop("PDT_PLANS_DIR", None)
    # The rest of the guard's inputs, for the same reason.  A sub-agent
    # session exports the whole set (the task's declaration slip, the
    # read-only stamp, the report carve-out, the lock identity), and a
    # stale value changes what the guard decides -- the case under test is
    # about containment, not about whatever the launching shell happened
    # to declare.
    for _leaked in (
        "PDT_REPORT_PATH",
        "PDT_READONLY_TASK",
        "PDT_ALLOWED_WRITE_FILES",
        "PDT_UNDECLARED_WRITE_DIR",
        "PDT_DECLARED_MANIFEST_HASH",
        "PDT_LOCK_CLI",
        "PDT_LOCK_BROKER",
        "PDT_LOCK_TASK_ID",
    ):
        env.pop(_leaked, None)
    if pdt_project_dir:
        env["PDT_PROJECT_DIR"] = pdt_project_dir
    if pdt_formal_repo:
        env["PDT_FORMAL_REPO_PATH"] = pdt_formal_repo
    if pdt_plans_dir:
        env["PDT_PLANS_DIR"] = pdt_plans_dir
    payload = json.dumps({"tool_input": {"file_path": file_path}})
    proc = subprocess.run(
        ["python3", str(guard)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        # The budget is the greater of the two sides' numbers, so that
        # neither guarantee is given up.  This branch's budget scales
        # with the host's load average: a busy host gets more than the
        # five seconds a quiet one gets.  Main's gate hardening pinned
        # a flat 60 s, which outlasts the gate's own 18-way xdist
        # parallelism -- the guard was measured at 9.1-16.2 s under it,
        # so a 5 s ceiling produced a red that said nothing about the
        # containment rule.  Taking the greater of the two never gives
        # the guard less than main's floor, and the scaled budget never
        # exceeds the branch's own 60 s ceiling -- the two agree at
        # 60 s today, and either one may be moved later without the
        # other silently overriding it.
        timeout=max(_guard_timeout_seconds(), _GUARD_TIMEOUT_SECONDS),
    )
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"guard exited {proc.returncode}; stderr={proc.stderr!r}"
        )
    return proc.returncode


@pytest.fixture
def dev_checkout(tmp_path):
    """A stand-in for a dev checkout."""
    p = tmp_path / "dev-checkout"
    p.mkdir()
    (p / "src").mkdir()
    return p


@pytest.fixture
def plans_dir(tmp_path):
    p = tmp_path / "plans"
    p.mkdir()
    (p / "20260823-plan").mkdir()
    return p


# ----- happy paths: writes inside allowed zones ----------------------------


def test_allows_edit_inside_project_dir(dev_checkout):
    """A sub-agent must be able to edit code in its own checkout."""
    target = dev_checkout / "src" / "data_updater.py"
    rc = _run_guard(str(target), pdt_project_dir=str(dev_checkout))
    assert rc == 0, "Edit inside project_dir must be allowed"


def test_allows_edit_inside_plans_dir(dev_checkout, plans_dir):
    """Plan state files in the shared plans/ dir are always allowed."""
    target = plans_dir / "20260823-plan" / "tasks.json"
    rc = _run_guard(
        str(target),
        pdt_project_dir=str(dev_checkout),
        pdt_plans_dir=str(plans_dir),
    )
    assert rc == 0, "Edit inside plans/ must be allowed"


def test_allows_write_in_tempdir(dev_checkout):
    """Sub-agent settings file and activity log live under tempdir."""
    target = "/tmp/subagent_settings_abc123.json"
    rc = _run_guard(str(target), pdt_project_dir=str(dev_checkout))
    assert rc == 0, "Edit inside /tmp must be allowed"


def test_allows_write_in_private_tmp(dev_checkout):
    """macOS uses /private/tmp which /tmp symlinks to."""
    target = "/private/tmp/subagent_log_xyz.log"
    rc = _run_guard(str(target), pdt_project_dir=str(dev_checkout))
    assert rc == 0, "Edit inside /private/tmp must be allowed"


# ----- the critical incident regression -----------------------------------


def test_blocks_edit_outside_project_dir_to_production_repo(dev_checkout):
    """The 2026-08-26 incident: project_dir=dev-checkout, but a sub-agent
    must NOT be able to write to the formal/deploy production checkout.
    Path is derived from the launch home (``_real_home()``) rather than
    hard-coded so the test is portable across developer machines."""
    target = str(_FOREIGN_CHECKOUT_DIR / "src" / "stock_data" / "data_updater.py")
    rc = _run_guard(target, pdt_project_dir=str(dev_checkout))
    assert rc == 2, \
        "Edit to production must be blocked when project_dir=dev-checkout"


def test_blocks_edit_outside_project_dir_to_arbitrary_path(dev_checkout):
    """Containment: a sub-agent cannot write anywhere outside its own
    checkout, plans/, or temp."""
    target = str(
        _FOREIGN_CHECKOUT_DIR.parent / "other-repo" / "whatever.txt"
    )
    rc = _run_guard(target, pdt_project_dir=str(dev_checkout))
    assert rc == 2, "Edit to an earlier plan must be blocked"


def test_blocks_edit_to_formal_ac_repo(dev_checkout):
    """The sub-agent must not be able to edit the backend's own formal repo either.
    Path derived from the launch home (``_real_home()``) rather than hard-coded."""
    target = str(_FOREIGN_PRODUCTION_DIR / "backend" / "server.py")
    rc = _run_guard(target, pdt_project_dir=str(dev_checkout))
    assert rc == 2, "Edit to the checkout the backend runs from must be blocked"


# ----- edge cases --------------------------------------------------------


def test_blocks_when_file_path_resolves_through_symlink(dev_checkout, tmp_path):
    """If a symlink under project_dir/ points at the formal repo, the
    resolved real path is what matters."""
    link = dev_checkout / "leak"
    link.symlink_to(str(_FOREIGN_CHECKOUT_DIR))
    target = link / "src" / "data_updater.py"
    rc = _run_guard(str(target), pdt_project_dir=str(dev_checkout))
    assert rc == 2, "Symlink escape from project_dir must be blocked"


def test_blocks_edit_to_sibling_directory(dev_checkout, tmp_path):
    """A sibling dir of project_dir (same parent) must NOT be writable.

    Note: pytest's ``tmp_path`` lives under macOS's ``tempfile.gettempdir()``
    which the guard deliberately allows as scratch space. To assert the
    containment rule for *non-temp* siblings we mount project_dir under
    the home this run was launched with, which is outside tempdir and the
    checkout the backend runs from -- ``_real_home()``, not ``Path.home()``,
    because the redirected one *is* a tempdir path and would make this case
    allow the sibling for the wrong reason.
    """
    import shutil
    parent = _real_home() / ".pdt-guard-tests"
    if parent.exists():
        shutil.rmtree(parent)
    parent.mkdir()
    project = parent / "dev-checkout"
    sibling = parent / "other-repo"
    project.mkdir()
    sibling.mkdir()
    target = sibling / "src" / "data_updater.py"
    rc = _run_guard(str(target), pdt_project_dir=str(project))
    assert rc == 2, f"Edit to a non-temp sibling dir must be blocked (got rc={rc})"


# ----- backwards compat: no project_dir ----------------------------------


def test_falls_back_to_formal_only_block_when_project_dir_unknown(tmp_path):
    """When PDT_PROJECT_DIR is unset (legacy behaviour), the guard
    reverts to blocking only writes to the checkout the backend runs from. Any other
    path is allowed."""
    formal = tmp_path / "formal-ac"
    formal.mkdir()
    other = tmp_path / "some-other-repo"
    other.mkdir()

    # Write to the served checkout -> blocked
    rc = _run_guard(str(formal / "x.py"), pdt_project_dir="",
                    pdt_formal_repo=str(formal))
    assert rc == 2, "Without project_dir, the served checkout writes must still be blocked"

    # Write anywhere else -> allowed
    rc = _run_guard(str(other / "x.py"), pdt_project_dir="",
                    pdt_formal_repo=str(formal))
    assert rc == 0, "Without project_dir, non-formal writes must be allowed"


# ----- guard lives inside subagent settings JSON ---------------------------


def test_guard_is_wired_into_settings_file():
    """Sanity: ``_build_guard_hook`` returns a ``python3 <tmpfile>``
    command whose tmpfile exists on disk and contains the containment
    rule.

    2026-09-14: load the production module as an ISOLATED copy instead
    of ``importlib.reload(coding_tool)``. The in-place reload rebinds
    ``coding_tool.ClaudeCodingTool`` to a NEW class object while
    sibling test modules imported during collection still hold the
    ORIGINAL class — their ``monkeypatch.setattr(ClaudeCodingTool,
    ...)`` seams then patch a class the production code paths no
    longer consult, and provider resolution silently falls through to
    the real cc-switch DB (bisected: this one reload caused all 12
    failures in ``test_coding_tool*.py`` in a full-suite run). Loading
    via ``spec_from_file_location`` under a throwaway module name
    exercises the same fresh-module code path without touching
    ``sys.modules['coding_tool']``.
    """
    import importlib.util

    import coding_tool

    module_path = Path(coding_tool.__file__).resolve()
    spec = importlib.util.spec_from_file_location(
        "_coding_tool_guard_isolation_check", module_path
    )
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)

    cmd = isolated._build_guard_hook()["command"]
    assert cmd.startswith("python3 "), cmd
    path = Path(cmd.split(" ", 1)[1])
    assert path.exists(), f"guard script {path} not on disk"
    body = path.read_text()
    assert "PDT_PROJECT_DIR" in body
    # The script gained a second stage (the file-lock broker) after this
    # assertion was written, and its single-line ``sys.exit(2 if blocked
    # else 0)`` became two branches. Pin the containment *decision* and
    # the exit code instead of one spelling of them — the point is that
    # an out-of-project edit is still refused with 2, which
    # ``test_refuses_write_outside_project`` also checks by running it.
    assert "blocked = not allowed" in body
    assert "sys.exit(2)" in body


# ----- the budget the guard is given ---------------------------------------


def test_the_guard_budget_scales_with_the_host_load(monkeypatch):
    """A busy host must not be measured by a fixed budget.

    The guard is a small script, but it is started with ``python3``: on a
    host whose load average was 60 a cold interpreter needed hundreds of
    milliseconds before it said anything at all, and a fixed five-second
    budget turned that into a ``TimeoutExpired`` -- a failure that says
    nothing whatsoever about the containment rule this file exists to
    pin.

    So the budget follows the host's load average.  It is also capped: a
    genuinely hung guard must still fail the test rather than hang it.
    """
    seen = {}
    real_run = subprocess.run

    def _capture(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _capture)
    monkeypatch.setattr(os, "getloadavg", lambda: (50.0, 40.0, 30.0))

    rc = _run_guard("/tmp/pdt-guard-budget-probe.json", pdt_project_dir="/tmp")

    assert rc in (0, 2), f"the guard answered {rc}"
    assert seen.get("timeout") is not None, (
        "the guard was run without a wall-clock budget at all"
    )
    assert seen["timeout"] >= 30.0, (
        f"the guard's budget was {seen['timeout']}s on a host whose load "
        f"average was 50; it must scale with the load"
    )


def test_the_guard_budget_stays_inside_its_ceiling(monkeypatch):
    """A scaled budget is still a budget.

    The scaling exists so that a busy host is not judged by a wall clock
    it cannot meet.  It must not turn into "wait forever": the ceiling
    keeps a hung guard a failing test.
    """
    seen = {}
    real_run = subprocess.run

    def _capture(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _capture)
    monkeypatch.setattr(os, "getloadavg", lambda: (100000.0, 100000.0, 100000.0))

    _run_guard("/tmp/pdt-guard-budget-ceiling.json", pdt_project_dir="/tmp")

    assert seen.get("timeout") is not None
    assert seen["timeout"] <= 60.0, (
        f"the guard's budget was {seen['timeout']}s; a scaled budget still "
        f"needs a ceiling so that a hung guard fails instead of hanging"
    )