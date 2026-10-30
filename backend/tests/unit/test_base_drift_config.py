"""Configuration, report shape and event names of the base-drift check.

The base-drift check answers one question before a verification round
starts: *is this branch still in sync with its base?*  A branch that
drifted from ``main`` for weeks verifies a codebase that no longer
exists upstream — the round then fails on symptoms (a test that no
longer matches the current base) instead of on the cause.

This module pins the three things the rest of the feature leans on,
and nothing else:

* the **configuration surface** — three environment knobs, each with a
  safe default.  A typo in a value must fall back to the default
  instead of raising: a base-drift check that refuses to start because
  somebody wrote ``PDT_BASE_DRIFT_MODE=repairp`` is a check that never
  runs, and the drift it exists to catch stays invisible.
* the **report shape** — every status produces the same JSON keys, so
  a consumer never has to ask "does this event carry a ``base_sha``?".
  ``base_ref`` and ``base_sha`` are always present, even when empty:
  an absent key and an empty value mean different things to whoever
  reads the event log afterwards.
* the **event names** — one stable name per status, so the event log
  can be grepped and counted.

The status set is closed (seven statuses).  ``skipped`` is the odd one
out: it means "this directory is not a git work tree", which is an
internal condition and *not* a failure — the round continues.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
from base_drift import (  # noqa: E402
    EVENT_BY_STATUS,
    STATUS_CONFLICTS,
    STATUS_DIRTY_TREE,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_NONE,
    STATUS_SKIPPED,
    BaseDriftConfig,
    BaseDriftReport,
)

#: The repository root's ``.env.example``.  It lives three levels up from
#: this file (``backend/tests/unit/`` -> ``backend/tests/`` -> ``backend/``
#: -> repository root).  A *relative* path cannot be used here: the suite
#: is executed as ``cd backend && pytest tests/unit/...``, so the process
#: working directory is ``backend/`` and ``".env.example"`` would resolve
#: to ``backend/.env.example`` — a file that does not exist.
REPO_ROOT_ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"

ALL_STATUSES = (
    STATUS_NONE,
    STATUS_MERGED,
    STATUS_CONFLICTS,
    STATUS_DIRTY_TREE,
    STATUS_FAILED,
    STATUS_DISABLED,
    STATUS_SKIPPED,
)

#: The knobs a deployment may turn.  All three are optional; each one has
#: a default that keeps the check enabled and read-only.
EXPECTED_KNOBS = (
    "PDT_BASE_DRIFT_ENABLED",
    "PDT_BASE_DRIFT_BASE",
    "PDT_BASE_DRIFT_MODE",
)


def test_defaults():
    """An empty environment yields the documented, safe defaults.

    "Safe" means the check runs but changes nothing: it is enabled, it
    compares against ``origin/main``, and it only *reports*.
    """
    cfg = BaseDriftConfig.from_env({})

    assert isinstance(cfg, BaseDriftConfig)
    assert cfg.enabled is True
    assert cfg.base_ref == "origin/main"
    assert cfg.mode == "report"
    assert cfg.timeout_sec == 120


def test_env_overrides_and_falsy_enabled():
    """The knobs override the defaults, and the off switch really turns it off."""
    cfg = BaseDriftConfig.from_env(
        {"PDT_BASE_DRIFT_BASE": "upstream/main", "PDT_BASE_DRIFT_MODE": "repair"}
    )

    assert (cfg.base_ref, cfg.mode) == ("upstream/main", "repair")

    # Every spelling of "off" disables the check.  Case is irrelevant:
    # `False`, `FALSE` and `False` are the same answer.
    for falsy in ("0", "false", "FALSE", "False", "no", "NO", "off", "OFF"):
        assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_ENABLED": falsy}).enabled is False

    # ... and everything else leaves it on.  A junk value must not
    # silently disable the check either.
    for truthy in ("1", "true", "TRUE", "yes", "on", "", "maybe"):
        assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_ENABLED": truthy}).enabled is True


def test_invalid_mode_falls_back_to_report():
    """An unknown mode falls back to ``report`` — the read-only mode.

    ``repair`` is the only mode that acts on the working tree, so an
    unrecognised value must never be trusted with it.
    """
    assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_MODE": "repairp"}).mode == "report"
    assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_MODE": "nonsense"}).mode == "report"
    assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_MODE": ""}).mode == "report"
    assert BaseDriftConfig.from_env({"PDT_BASE_DRIFT_MODE": "REPAIR"}).mode == "repair"


def test_report_to_dict_field_completeness():
    """Every status produces the same JSON keys.

    A consumer of the event log must not have to ask "does this event
    carry a ``base_sha``?" — the key is there whether or not it has a
    value.
    """
    for status in ALL_STATUSES:
        report = BaseDriftReport(
            status=status,
            base_ref="origin/main",
            base_sha="",
            behind=0,
            ahead=0,
            stderr_excerpt="",
            conflict_files=[],
            mode="report",
            detail="",
        )

        payload = report.to_dict()

        assert payload["status"] == status
        for field in (
            "base_ref",
            "base_sha",
            "behind",
            "ahead",
            "stderr_excerpt",
            "conflict_files",
            "mode",
        ):
            assert field in payload, f"{status} is missing {field}"
        assert payload["base_ref"] == "origin/main"
        assert payload["base_sha"] == ""  # an empty value is still a value
        assert payload["behind"] == 0
        assert payload["ahead"] == 0
        assert payload["stderr_excerpt"] == ""
        assert payload["conflict_files"] == []
        assert payload["mode"] == "report"
        assert payload["detail"] == ""


def test_report_to_dict_matches_the_documented_shape():
    """The default report serialises to exactly the documented payload."""
    report = BaseDriftReport(status=STATUS_NONE, base_ref="origin/main", mode="report")

    assert report.to_dict() == {
        "status": "none",
        "base_ref": "origin/main",
        "base_sha": "",
        "behind": 0,
        "ahead": 0,
        "stderr_excerpt": "",
        "conflict_files": [],
        "mode": "report",
        "detail": "",
    }


def test_event_name_per_status():
    """One stable event name per status, and the mapping is total."""
    assert EVENT_BY_STATUS == {
        "none": "base_drift_none",
        "merged": "base_drift_merged",
        "conflicts": "base_drift_conflicts",
        "dirty_tree": "base_drift_dirty_tree",
        "failed": "base_drift_failed",
        "disabled": "base_drift_disabled",
        "skipped": "base_drift_skipped",
    }
    assert set(EVENT_BY_STATUS) == set(ALL_STATUSES)

    for status in ALL_STATUSES:
        assert BaseDriftReport(status=status).event == EVENT_BY_STATUS[status]


def test_only_conflicts_block_the_round():
    """A round is blocked by conflicts and by nothing else.

    ``skipped`` is an internal condition ("not a git work tree"), not a
    failure — it must not stop the round.
    """
    assert BaseDriftReport(status=STATUS_CONFLICTS).blocked is True

    for status in (
        STATUS_NONE,
        STATUS_MERGED,
        STATUS_DIRTY_TREE,
        STATUS_FAILED,
        STATUS_DISABLED,
        STATUS_SKIPPED,
    ):
        assert BaseDriftReport(status=status).blocked is False


def test_env_example_documents_knobs():
    """The knobs are discoverable in the committed ``.env.example``."""
    assert REPO_ROOT_ENV_EXAMPLE.exists(), f"missing {REPO_ROOT_ENV_EXAMPLE}"

    text = REPO_ROOT_ENV_EXAMPLE.read_text(encoding="utf-8")

    for knob in EXPECTED_KNOBS:
        assert knob in text, f"{knob} is not documented in .env.example"


def test_module_exposes_the_documented_surface():
    """The public names the rest of the feature imports are all there."""
    for name in (
        "BaseDriftConfig",
        "BaseDriftReport",
        "EVENT_BY_STATUS",
        "STATUS_NONE",
        "STATUS_MERGED",
        "STATUS_CONFLICTS",
        "STATUS_DIRTY_TREE",
        "STATUS_FAILED",
        "STATUS_DISABLED",
        "STATUS_SKIPPED",
    ):
        assert hasattr(base_drift, name), f"base_drift.{name} is missing"
