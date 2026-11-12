"""The plan's context is two facts, and everything else is derived.

The context used to be four environment variables, three of them the
same fact: the plan's id, the plans root, the plan's own directory, and
a second copy of the plan's own directory for the verification
sub-agent.  A copy of a fact is a cache, and a cache drifts -- when the
two copies named different directories, one plan had two ledgers and
the backend's sweep could not see the plan's sub-agents at all.

So there are two truths now -- ``PDT_PLAN_ID`` and ``PDT_PLANS_DIR``
(absolute) -- and the plan's directory is derived from them by
:mod:`plan_env`.  This file pins that:

* what the two truths are, and what the derivation does with them;
* that the plan's directory really is derived, in the process ledger
  that has to land in exactly one directory per plan;
* that the test harness moves the whole group, not one variable of it;
* that the copy that is left over is gone, in the code and in the
  settings the verification sub-agent runs with.

The grep at the end is the one that keeps it that way: the plan's
directory is not a variable any more, and a name that has been retired
does not creep back in as a second spelling of the same fact.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import _pytest_hermetic_env as hermetic  # noqa: E402
import plan_env  # noqa: E402
import sub_agent_registry  # noqa: E402
from plan_env import PLAN_ID_ENV, PLANS_DIR_ENV  # noqa: E402


def _is_under(child, parent) -> bool:
    """True when ``child`` is ``parent`` or lives under it.

    Both sides resolved: on macOS ``/tmp`` is a symlink to
    ``/private/tmp``, so a string comparison would call a path inside
    the session root "outside" it.
    """
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except (ValueError, OSError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 1. The two truths, and the pair they are written as.
# ---------------------------------------------------------------------------


def test_plan_env_writes_the_two_truths_and_an_absolute_root(tmp_path):
    """``plan_env()`` writes both halves, and the root is absolute."""
    plans_root = tmp_path / "plans"

    env = plan_env.plan_env("20260101-a-plan", plans_root)

    assert env[PLAN_ID_ENV] == "20260101-a-plan"
    assert env[PLANS_DIR_ENV] == str(plans_root.resolve())
    assert Path(env[PLANS_DIR_ENV]).is_absolute()


def test_plan_env_without_a_plan_id_writes_no_plan_id(tmp_path):
    """No plan id → no plan id key.  An empty one is no plan."""
    env = plan_env.plan_env(None, tmp_path / "plans")

    assert PLAN_ID_ENV not in env
    assert PLANS_DIR_ENV in env

    env = plan_env.plan_env("", tmp_path / "plans")

    assert PLAN_ID_ENV not in env


# ---------------------------------------------------------------------------
# 2. Reading the root: a relative value is no value.
# ---------------------------------------------------------------------------


def test_plans_dir_refuses_a_relative_root(monkeypatch):
    """A relative root names a different tree in every working directory.

    The executor's working directory is ``backend/``; the backend's is
    not.  A relative root would therefore answer "one directory" for
    the backend and "another" for everything it starts.
    """
    monkeypatch.setenv(PLANS_DIR_ENV, "plans")

    assert plan_env.plans_dir() is None


def test_plans_dir_reads_an_absolute_root(monkeypatch, tmp_path):
    """An absolute root is read back as it is."""
    monkeypatch.setenv(PLANS_DIR_ENV, str(tmp_path / "plans"))

    assert plan_env.plans_dir() == tmp_path / "plans"


# ---------------------------------------------------------------------------
# 3. The derivation.
# ---------------------------------------------------------------------------


def test_plan_dir_for_joins_the_two_truths(monkeypatch, tmp_path):
    """``<plans root>/<plan id>``."""
    monkeypatch.setenv(PLANS_DIR_ENV, str(tmp_path / "plans"))
    monkeypatch.setenv(PLAN_ID_ENV, "20260101-a-plan")

    assert plan_env.plan_dir_for() == tmp_path / "plans" / "20260101-a-plan"


def test_plan_dir_for_needs_both_halves(monkeypatch, tmp_path):
    """One half alone is not a plan directory."""
    monkeypatch.setenv(PLANS_DIR_ENV, str(tmp_path / "plans"))
    monkeypatch.delenv(PLAN_ID_ENV, raising=False)
    assert plan_env.plan_dir_for() is None

    monkeypatch.setenv(PLAN_ID_ENV, "20260101-a-plan")
    monkeypatch.delenv(PLANS_DIR_ENV, raising=False)
    assert plan_env.plan_dir_for() is None

    # A relative root is no root at all.
    monkeypatch.setenv(PLANS_DIR_ENV, "plans")
    assert plan_env.plan_dir_for() is None


# ---------------------------------------------------------------------------
# 4. The registry derives the plan's directory from the two truths.
# ---------------------------------------------------------------------------


def test_the_registry_derives_the_plan_directory_from_the_two_truths(
    monkeypatch, tmp_path
):
    """``PDT_PLANS_DIR`` + ``PDT_PLAN_ID`` are enough to name the directory."""
    plan_id = "20260101-a-plan"
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True)

    monkeypatch.setenv(PLANS_DIR_ENV, str(plans_root))
    monkeypatch.setenv(PLAN_ID_ENV, plan_id)

    assert sub_agent_registry._plan_dir_from_env(plan_id) == plan_dir


def test_the_registry_refuses_a_context_that_names_another_plan(
    monkeypatch, tmp_path
):
    """The plan id in the environment speaks for that plan only.

    The environment is inherited by everything the executor starts, so
    a context that names a *different* plan says nothing about this one.
    """
    plan_id = "20260101-a-plan"
    other_plan_id = "20260101-somebody-else"
    plans_root = tmp_path / "plans"
    (plans_root / plan_id).mkdir(parents=True)
    (plans_root / other_plan_id).mkdir(parents=True)

    monkeypatch.setenv(PLANS_DIR_ENV, str(plans_root))
    monkeypatch.setenv(PLAN_ID_ENV, other_plan_id)

    assert sub_agent_registry._plan_dir_from_env(plan_id) is None


def test_the_registry_refuses_a_plan_directory_that_is_not_there(
    monkeypatch, tmp_path
):
    """The two truths can name a directory that does not exist.

    Whether the directory has to exist is the caller's own semantics;
    the registry's is that a plan without a directory has no ledger.
    """
    plan_id = "20260101-a-plan"
    plans_root = tmp_path / "plans"
    plans_root.mkdir()

    monkeypatch.setenv(PLANS_DIR_ENV, str(plans_root))
    monkeypatch.setenv(PLAN_ID_ENV, plan_id)

    assert sub_agent_registry._plan_dir_from_env(plan_id) is None


# ---------------------------------------------------------------------------
# 5. The test harness moves the whole group, not one variable of it.
# ---------------------------------------------------------------------------


def _a_live_tree(tmp_path: Path) -> tuple[Path, str]:
    """A plans tree that is not the suite's own: a live plan's tree."""
    plans_root = tmp_path / "live" / "plans"
    plan_id = "20260101-a-live-plan"
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"requirement": "live", "tasks": []}), encoding="utf-8"
    )
    return plans_root, plan_id


def test_the_plan_group_is_redirected_when_the_suite_inherits_a_live_tree(
    monkeypatch, tmp_path
):
    """A live plans root drags the whole group into the session root.

    The plan context is one fact in two halves.  A suite that inherits
    the plans root of somebody else's live tree has to be moved out of
    it -- root *and* plan id, so the two cannot disagree about which
    plan this is.
    """
    live_root, live_plan_id = _a_live_tree(tmp_path)
    session_root = Path(os.environ["PDT_TEST_SESSION_ROOT"])

    monkeypatch.setenv(PLANS_DIR_ENV, str(live_root))
    monkeypatch.setenv(PLAN_ID_ENV, live_plan_id)
    monkeypatch.setattr(hermetic, "_INSTALLED_ENV", None)

    hermetic.install_hermetic_env()

    redirected = Path(os.environ[PLANS_DIR_ENV])
    assert redirected != live_root, (
        "the suite kept the live plans root — it would scan the live tree"
    )
    assert _is_under(redirected, session_root), (
        f"the plans root was not moved into the session root: {redirected!s}"
    )
    derived = plan_env.plan_dir_for()
    assert derived is not None
    assert _is_under(derived, session_root), (
        f"the plan's directory still points outside the session root: "
        f"{derived!s}"
    )


def test_the_group_redirect_is_what_moves_the_plan_context(
    monkeypatch, tmp_path
):
    """The negative control: without the redirect, the live tree stays.

    This is the case that proves the assertion above is measuring the
    redirect and not something else: the same live root, the same
    session root, and the escape hatch set -- so the group is *not*
    moved and the value stays where it was.
    """
    live_root, live_plan_id = _a_live_tree(tmp_path)

    monkeypatch.setenv(PLANS_DIR_ENV, str(live_root))
    monkeypatch.setenv(PLAN_ID_ENV, live_plan_id)
    monkeypatch.setenv("PDT_TEST_ALLOW_LIVE_PLANS", "1")
    monkeypatch.setattr(hermetic, "_INSTALLED_ENV", None)

    hermetic.install_hermetic_env()

    assert Path(os.environ[PLANS_DIR_ENV]) == live_root, (
        "with the escape hatch set the live root is kept — so the redirect "
        "in the case above is the thing that moved it"
    )
    assert os.environ[PLAN_ID_ENV] == live_plan_id


# ---------------------------------------------------------------------------
# 6. The copy that is left over is gone.
# ---------------------------------------------------------------------------


def test_the_verification_settings_name_the_plan_directory_once():
    """``PDT_VP_WRITE_SCOPE`` is the plan directory; there is no second copy."""
    from verification_subagent import SettingsJsonBuilder

    plan_dir = Path(tempfile.mkdtemp(prefix="vp-plan-"))
    builder = SettingsJsonBuilder(vp_id="VP-001", plan_id="p", plan_dir=plan_dir)
    path = builder.build()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        builder.cleanup()

    env = data.get("env", {})
    assert env.get("PDT_VP_WRITE_SCOPE") == str(plan_dir)
    assert "PDT_VP_PLAN_DIR" not in env, (
        f"the plan directory is written twice: {sorted(env)}"
    )


# ---------------------------------------------------------------------------
# 7. The retired name does not come back.
# ---------------------------------------------------------------------------


def test_the_retired_plan_directory_variable_is_gone_from_the_code():
    """No production code mentions the plan's directory as its own variable.

    The plan's directory is derived now.  A variable that carries it
    again would be a third copy of a fact that already drifted once.
    """
    offenders = []
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        parts = path.relative_to(BACKEND_DIR).parts
        if "tests" in parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "PDT_PLAN_DIR" in text:
            offenders.append(str(path.relative_to(BACKEND_DIR)))

    assert not offenders, (
        f"the retired variable is back in production code: {offenders!r}"
    )
