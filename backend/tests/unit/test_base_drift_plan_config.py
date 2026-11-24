"""The per-plan configuration of the base-drift check.

The base ref is a property of the **repository**, not of the deployment.
A deployment may host several plans against several repositories, and
those repositories do not agree on what their base is called: the
remote may be ``origin`` or ``github``, the default branch may be
``main`` or ``master``.  One environment variable cannot carry that.

So each plan states its own base, in ``plans/<plan_id>/base_drift.json``,
and the environment stays the fallback for every plan that says
nothing.

What this file pins:

* the **precedence** — the plan's file wins over the environment, and
  the environment wins over the default.  A plan that says nothing
  keeps exactly the behaviour it had before the file existed.
* the **failure mode** — a missing file, a file that is not JSON, a
  JSON document that is not an object, a key of the wrong type: each
  falls back to the layer underneath, key by key, and **none of them
  raises**.  This is the same stance the environment knobs take, and
  for the same reason: a typo in the configuration must not disable
  the check, and must not break the round that reads it.
* the **end-to-end effect** — a repository whose remote is ``github``
  really is measured, and really is merged, when the plan says so.
  Without the configuration it really is not measured at all.

The last one is the point of the whole feature.  A check that reports
"the base could not be resolved" every round has measured nothing, and
a plan that cannot say which base it uses cannot be checked at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    PLAN_CONFIG_FILENAME,
    STATUS_FAILED,
    STATUS_MERGED,
    BaseDriftConfig,
)

#: The identity every commit in these tests carries.  It is passed with
#: ``-c`` instead of being configured globally: the suite runs with
#: ``$HOME`` pointed at a scratch directory, where ``user.email`` is not
#: set, and a commit without an identity fails outright.
GIT_IDENTITY = (
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=t",
    "-c",
    "commit.gpgsign=false",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command inside ``repo`` and fail loudly if it errors."""
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENTITY, *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _write_plan_config(plan_dir: Path, payload: object) -> Path:
    """Write the plan's ``base_drift.json`` and return the plan directory."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / PLAN_CONFIG_FILENAME
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return plan_dir


# ---------------------------------------------------------------------------
# The layering: plan file over environment over default
# ---------------------------------------------------------------------------


def test_plan_file_base_ref_overrides_the_environment(tmp_path):
    """The plan's own base ref wins over ``PDT_BASE_DRIFT_BASE``.

    This is the whole point of the feature: the environment says
    ``origin/main`` because that is the default for a deployment, and
    *this* plan works against a repository whose remote is called
    something else.  The plan is the more specific statement, so the
    plan wins.
    """
    plan_dir = _write_plan_config(tmp_path / "plan", {"base_ref": "github/master"})

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir,
        environ={"PDT_BASE_DRIFT_BASE": "origin/main"},
    )

    assert cfg.base_ref == "github/master"


def test_no_plan_file_uses_the_environment(tmp_path):
    """A plan that says nothing keeps exactly what it had.

    The plan directory here does not exist at all — the common case.
    The environment's value is used, and the default is used when the
    environment says nothing either.
    """
    plan_dir = tmp_path / "plan"

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir, environ={"PDT_BASE_DRIFT_BASE": "upstream/main"}
    )

    assert cfg.base_ref == "upstream/main"

    # No environment either: the documented default.
    default = BaseDriftConfig.from_plan_dir(plan_dir, environ={})
    assert default.base_ref == "origin/main"
    assert default.mode == "report"
    assert default.enabled is True


def test_typo_in_mode_falls_back_without_raising(tmp_path):
    """A misspelled mode falls back to the layer underneath — and does not raise.

    ``repairp`` is not a mode.  Handing it the working tree would be
    worse than ignoring it, so the value below it is kept: the
    environment's mode when there is one, the default ``report``
    otherwise.

    The "does not raise" half is the load-bearing half: a
    configuration file that breaks the verification round is worse than
    a configuration file that is ignored.
    """
    plan_dir = _write_plan_config(tmp_path / "plan", {"mode": "repairp"})

    cfg = BaseDriftConfig.from_plan_dir(plan_dir, environ={})

    assert cfg.mode == "report"

    # The environment's mode is the layer underneath, so it survives.
    from_env = BaseDriftConfig.from_plan_dir(
        plan_dir, environ={"PDT_BASE_DRIFT_MODE": "repair"}
    )
    assert from_env.mode == "repair"


def test_broken_json_falls_back_to_the_environment_without_raising(tmp_path):
    """A file that is not JSON says nothing at all.

    Every key falls back to the environment, and the call returns
    normally — a half-written configuration file must not take the
    verification round down with it.
    """
    plan_dir = _write_plan_config(tmp_path / "plan", "{ this is not json")

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir,
        environ={
            "PDT_BASE_DRIFT_BASE": "upstream/main",
            "PDT_BASE_DRIFT_MODE": "repair",
        },
    )

    assert cfg.base_ref == "upstream/main"
    assert cfg.mode == "repair"
    assert cfg.enabled is True


def test_enabled_false_in_the_plan_file(tmp_path):
    """``enabled: false`` in the plan file switches the check off.

    A plan may opt out of the check entirely — and the opt-out is
    written as a real JSON boolean, so there is no spelling to get
    wrong.
    """
    plan_dir = _write_plan_config(tmp_path / "plan", {"enabled": False})

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir, environ={"PDT_BASE_DRIFT_ENABLED": "1"}
    )

    assert cfg.enabled is False


def test_wrong_types_and_unknown_keys_are_ignored(tmp_path):
    """A key of the wrong type is ignored; an unknown key changes nothing.

    Each key is judged on its own, so one bad key does not take the
    good ones down with it.  And a file written for a newer version of
    the module must not break an older one.
    """
    plan_dir = _write_plan_config(
        tmp_path / "plan",
        {
            "enabled": "false",          # a string, not a boolean
            "base_ref": "",              # empty — nothing to name
            "mode": "repair",            # good
            "something_new": {"a": 1},  # unknown key
        },
    )

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir,
        environ={"PDT_BASE_DRIFT_BASE": "upstream/main", "PDT_BASE_DRIFT_ENABLED": "0"},
    )

    # The bad keys kept the layer underneath; the good key applied.
    assert cfg.enabled is False          # from the environment, not from "false"
    assert cfg.base_ref == "upstream/main"
    assert cfg.mode == "repair"


def test_a_json_document_that_is_not_an_object_is_ignored(tmp_path):
    """A JSON array is not a configuration object; it says nothing."""
    plan_dir = _write_plan_config(tmp_path / "plan", ["github/master"])

    cfg = BaseDriftConfig.from_plan_dir(
        plan_dir, environ={"PDT_BASE_DRIFT_BASE": "upstream/main"}
    )

    assert cfg.base_ref == "upstream/main"


# ---------------------------------------------------------------------------
# End to end: the configuration really changes what the check measures
# ---------------------------------------------------------------------------


def _repo_with_github_remote(tmp_path: Path) -> Path:
    """A work tree whose remote is called ``github``, behind by one commit.

    This is the shape that made the check useless: the remote is not
    ``origin`` and the default branch is not ``main``, so
    ``git rev-parse origin/main`` fails and the check measured nothing
    on every single round.

    The base is a **local bare repository** reached through a remote
    pointing at a path on disk — the fetch inside the check really
    runs, and it never touches the network.
    """
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "master"],
        cwd=str(source),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(source, "commit", "--allow-empty", "-m", "initial")

    bare = tmp_path / "github.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        text=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "feat"],
        cwd=str(work),
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "remote", "add", "github", str(bare))
    _git(work, "fetch", "github", "master")
    _git(work, "reset", "--hard", "github/master")

    # The base moves on by one commit; the work tree fetches it.
    _git(source, "commit", "--allow-empty", "-m", "base moved on")
    subprocess.run(
        ["git", "-C", str(source), "push", str(bare), "master:master"],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(work, "fetch", "github", "master")

    return work


def test_configured_base_ref_is_measured_and_merged(tmp_path):
    """With the base ref configured, the drift is really measured.

    ``github/master`` is the base this repository actually has, so the
    check finds the one-commit drift and merges the base in.  The
    report says ``merged`` and ``behind == 1``.
    """
    work = _repo_with_github_remote(tmp_path)

    report = base_drift.check_and_merge_base(
        work, config=BaseDriftConfig(base_ref="github/master")
    )

    assert report.status == STATUS_MERGED
    assert report.base_ref == "github/master"
    assert report.behind == 1
    assert report.merge_commit != ""

    # The base really is in the branch's history now.
    merged = _git(work, "merge-base", "--is-ancestor", "github/master", "HEAD")
    assert merged.returncode == 0


def test_without_the_configuration_the_drift_is_not_measured(tmp_path):
    """The negative control: without the configuration, nothing is measured.

    The same repository, the default configuration — ``origin/main``,
    which this repository does not have.  The check fails, and it says
    so in git's own words.  It does **not** report "in sync", and it
    does **not** measure the drift: the drift against ``github/master``
    is real and stays unmeasured.

    This is what makes the test above a fact rather than a claim: the
    difference between the two runs is the configuration, nothing else.
    """
    work = _repo_with_github_remote(tmp_path)

    report = base_drift.check_and_merge_base(work, config=BaseDriftConfig())

    assert report.status == STATUS_FAILED
    assert report.base_ref == "origin/main"
    assert report.behind == 0
    assert "Needed a single revision" in report.stderr_excerpt

    # The branch was not touched — nothing was merged.
    assert report.merge_commit == ""


def test_the_failed_check_says_which_remotes_the_repository_has(tmp_path):
    """A base ref that does not resolve is explained, not just reported.

    The complaint says the ref does not resolve.  The *detail* says
    what the repository does have — the remotes it is configured with
    and the remote-tracking refs it knows — so whoever reads the event
    log can write the right ``base_ref`` without going to look.

    The hint does not pick one: it lists what is there.  Choosing is
    the plan's business.
    """
    work = _repo_with_github_remote(tmp_path)

    report = base_drift.check_and_merge_base(work, config=BaseDriftConfig())

    assert report.status == STATUS_FAILED
    assert "github" in report.detail
    assert "github/master" in report.detail


def test_the_plan_file_drives_the_check_end_to_end(tmp_path):
    """The plan's file is what the check actually reads.

    This is the wiring, not the vocabulary: the configuration is built
    from the plan directory, handed to the check, and the check really
    merges the base this repository actually has.
    """
    work = _repo_with_github_remote(tmp_path)
    plan_dir = _write_plan_config(
        tmp_path / "plan", {"base_ref": "github/master"}
    )

    cfg = BaseDriftConfig.from_plan_dir(plan_dir, environ={})
    report = base_drift.check_and_merge_base(work, config=cfg)

    assert report.status == STATUS_MERGED
    assert report.behind == 1


def test_a_plan_without_a_config_behaves_exactly_as_before(tmp_path):
    """A plan that says nothing keeps the old behaviour, bit for bit.

    The configuration is additive: without a file the check reads the
    environment, and without the environment it reads the defaults.
    Nothing about the no-configuration case changed.
    """
    work = _repo_with_github_remote(tmp_path)
    plan_dir = tmp_path / "plan"

    cfg = BaseDriftConfig.from_plan_dir(plan_dir, environ={})

    assert cfg == BaseDriftConfig.from_env({})
    assert cfg.base_ref == "origin/main"
    assert cfg.mode == "report"
    assert cfg.enabled is True
    assert cfg.timeout_sec == BaseDriftConfig.from_env({}).timeout_sec


# ---------------------------------------------------------------------------
# The call site: the plan's file really reaches the check
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The one persistence surface the preflight touches."""

    def __init__(self) -> None:
        self.events = []
        self.reports = []

    def write_verification_point_log(self, vp_id, event_type, data):
        self.events.append((vp_id, event_type, data))

    def update_report(self, report_data):
        self.reports.append(report_data)


def _orchestrator(project_dir: Path, plan_dir: Path):
    """An orchestrator carrying only the attributes the preflight reads.

    Built with ``__new__`` on purpose: the preflight must not depend on
    a fully constructed orchestrator, and every attribute it touches is
    read defensively.
    """
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = project_dir
    orch.plan_dir = plan_dir
    orch.persistence = _FakePersistence()
    orch._current_round = 1
    orch._last_base_drift_report = None
    return orch


def test_the_preflight_reads_the_plan_s_own_base_ref(tmp_path):
    """The production entry point really uses the plan's configuration.

    This is the wiring, tested through the entry point the round
    actually calls — ``_run_base_drift_preflight`` — rather than
    through the configuration class alone.  A test of the class would
    pass even if the caller never handed the configuration over, and
    the whole feature would be dead code.

    The observable effect is a real merge: the work tree really is
    brought up to ``github/master``, and the event says which base was
    used.  If the call site stopped passing the plan's configuration,
    the check would look for ``origin/main``, fail, and this test would
    see a ``failed`` event instead of a merge.
    """
    work = _repo_with_github_remote(tmp_path)
    plan_dir = _write_plan_config(
        tmp_path / "plan", {"base_ref": "github/master"}
    )
    orch = _orchestrator(work, plan_dir)

    blocked = orch._run_base_drift_preflight()

    # The round went on — nothing conflicted.
    assert blocked is None, blocked

    # The event names the base the plan configured.
    vp_id, event_type, data = orch.persistence.events[0]
    assert vp_id == "__preflight__"
    assert event_type == "base_drift_merged"
    assert data["base_ref"] == "github/master"
    assert data["behind"] == 1

    # And the merge really happened in the work tree.
    merged = _git(work, "merge-base", "--is-ancestor", "github/master", "HEAD")
    assert merged.returncode == 0


def test_the_preflight_without_a_plan_file_keeps_the_old_behaviour(tmp_path):
    """A plan with no file is measured against ``origin/main`` as before.

    The same entry point, without a configuration file: the base is
    the default one, the check fails on this repository (it has no
    ``origin``), and the event says so.  Nothing about the
    no-configuration case changed.
    """
    work = _repo_with_github_remote(tmp_path)
    plan_dir = tmp_path / "plan"
    orch = _orchestrator(work, plan_dir)

    blocked = orch._run_base_drift_preflight()

    assert blocked is None, blocked
    vp_id, event_type, data = orch.persistence.events[0]
    assert event_type == "base_drift_failed"
    assert data["base_ref"] == "origin/main"
