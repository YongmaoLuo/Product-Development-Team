"""Tests for ``hooks.config`` — the per-plan HITL hook configuration.

What is being pinned
--------------------
The HITL hook configuration is a **plan property**: every plan carries its
own ``hitl_config.json`` next to the rest of that plan's state, and that
file is the only place the answer lives.  There is no cross-plan
inheritance and no global default file, because a plan created by
``curl`` and a plan created from a Claude Code session have genuinely
different answers to "who gets asked" — and a global default would give
both of them the same one.

Three properties carry the weight, and each of them is a way the
"no global fallback" rule can be broken by accident:

* **A missing file is a complete answer, not a missing answer.**  The
  defaults are ``auto`` / ``1800`` / no hooks, and they are produced by
  the absence of the file.  They are never looked up somewhere else.
* **The environment is not a source of truth.**  A stray
  ``PDT_HITL_MODE=strict`` in the operator's shell must not change how a
  plan behaves; the file is the only writer and the environment is not
  read at all.
* **``strict`` has to be declared.**  It is the mode where a human
  cannot be substituted, so it is only ever returned when the file
  itself says so — never as a side effect of a default chain.

The module path is pinned as well: ``backend/hooks/config.py`` with
``write_for_plan`` / ``load_for_plan`` at its surface, because the whole
point of the module is that there is exactly one reader and one writer
for this file.  Two copies of this logic would drift, and the drift
would show up as a plan whose stored config and read-back config
disagree.
"""

import json
import shlex
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hooks.config import (  # noqa: E402
    DEFAULT_MODE,
    DEFAULT_TIMEOUT_SEC,
    HITL_CONFIG_FILENAME,
    SCHEMA_VERSION,
    load_for_plan,
    write_for_plan,
)


# ---------------------------------------------------------------------------
# write_for_plan -> load_for_plan
# ---------------------------------------------------------------------------


def test_write_then_load(tmp_path):
    """What is written is what is read back."""
    plan_dir = tmp_path / "plan-1"

    written = write_for_plan(
        plan_dir,
        {"source": "plan_creation", "session_id": "s-1", "destination": "grp-1"},
    )

    assert written["version"] == SCHEMA_VERSION
    assert written["mode"] == DEFAULT_MODE
    assert written["timeout_sec"] == DEFAULT_TIMEOUT_SEC
    assert written["source"] == "plan_creation"

    # The hook is the delivery command itself, so what is asserted is
    # the argv the dispatcher will run: the delivery module, and the
    # project and session the plan was created for.  The argv is read
    # back with ``shlex.split`` rather than by re-implementing the
    # quoting — the command is what the dispatcher runs, so the argv is
    # the thing that has to be right.
    hooks = written["hooks"]
    assert len(hooks) == 1
    assert hooks[0]["kind"] == "cmd"
    assert shlex.split(hooks[0]["command"])[3:] == [
        "--project",
        "grp-1",
        "--session",
        "s-1",
    ]

    loaded = load_for_plan(plan_dir)

    assert loaded["version"] == SCHEMA_VERSION
    assert loaded["mode"] == DEFAULT_MODE
    assert loaded["timeout_sec"] == DEFAULT_TIMEOUT_SEC
    assert loaded["hooks"] == written["hooks"]

    # The provenance the load contract does not expose is still on disk:
    # it is what an operator reads to find out where a plan came from.
    on_disk = json.loads(
        (plan_dir / HITL_CONFIG_FILENAME).read_text(encoding="utf-8")
    )
    assert on_disk["source"] == "plan_creation"
    assert on_disk["captured_at"].endswith("Z")


# ---------------------------------------------------------------------------
# The defaults
# ---------------------------------------------------------------------------


def test_missing_defaults(tmp_path):
    """No file at all is a complete answer: auto, 1800, no hooks."""
    loaded = load_for_plan(tmp_path / "plan-that-never-ran")

    assert loaded == {
        "version": 1,
        "mode": "auto",
        "timeout_sec": 1800,
        "hooks": [],
    }


def test_corrupt_file_returns_defaults(tmp_path):
    """A file that cannot be read is treated as no file at all."""
    plan_dir = tmp_path / "plan-corrupt"
    plan_dir.mkdir()
    (plan_dir / HITL_CONFIG_FILENAME).write_text("{not json", encoding="utf-8")

    assert load_for_plan(plan_dir) == {
        "version": 1,
        "mode": "auto",
        "timeout_sec": 1800,
        "hooks": [],
    }


# ---------------------------------------------------------------------------
# No environment fallback
# ---------------------------------------------------------------------------


def test_no_env_fallback(tmp_path, monkeypatch):
    """The environment is never a source of truth for a plan's config."""
    monkeypatch.setenv("PDT_HITL_MODE", "strict")
    monkeypatch.setenv("PDT_HITL_TIMEOUT_SEC", "60")
    monkeypatch.setenv("PDT_HITL_SOURCE", "plan_creation")
    monkeypatch.setenv("PDT_HITL_DESTINATION", "grp-from-env")
    monkeypatch.setenv("HITL_MODE", "strict")
    monkeypatch.setenv("HITL_DESTINATION", "grp-from-env")

    plan_dir = tmp_path / "plan-env"

    # A plan with no file of its own stays on the defaults, whatever the
    # shell happens to say.
    assert load_for_plan(plan_dir) == {
        "version": 1,
        "mode": "auto",
        "timeout_sec": 1800,
        "hooks": [],
    }

    # And a plan that writes its own config does not inherit the
    # environment's destination either.
    write_for_plan(plan_dir, {"source": "plan_creation", "session_id": "s-1"})

    loaded = load_for_plan(plan_dir)
    assert loaded["mode"] == "auto"
    assert loaded["timeout_sec"] == 1800
    assert loaded["hooks"] == []


# ---------------------------------------------------------------------------
# The two ways a plan can be created
# ---------------------------------------------------------------------------


def test_empty_source_yields_no_hooks(tmp_path):
    """A plan created outside a session has nobody to ask."""
    plan_dir = tmp_path / "plan-curl"

    written = write_for_plan(plan_dir, {})

    assert written["source"] == ""
    assert written["hooks"] == []
    assert load_for_plan(plan_dir)["hooks"] == []


def test_destination_without_source_yields_no_hooks(tmp_path):
    """A hook needs a session to have come from as well as a target."""
    plan_dir = tmp_path / "plan-destination-only"

    written = write_for_plan(plan_dir, {"destination": "grp-1"})

    assert written["hooks"] == []


# ---------------------------------------------------------------------------
# strict is only ever returned when the file says so
# ---------------------------------------------------------------------------


def test_strict_only_when_explicitly_declared(tmp_path):
    """The mode is read from the file, and nowhere else."""
    plan_dir = tmp_path / "plan-strict"
    plan_dir.mkdir()
    (plan_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "strict",
                "timeout_sec": 60,
                "source": "plan_creation",
                "hooks": [],
            }
        ),
        encoding="utf-8",
    )

    assert load_for_plan(plan_dir) == {
        "version": 1,
        "mode": "strict",
        "timeout_sec": 60,
        "hooks": [],
    }

    # A file that does not declare a mode is an "auto" plan, even though
    # it is a file that exists.
    other_dir = tmp_path / "plan-no-mode"
    other_dir.mkdir()
    (other_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps({"version": 1, "timeout_sec": 60, "hooks": []}),
        encoding="utf-8",
    )

    assert load_for_plan(other_dir)["mode"] == "auto"


def test_unknown_mode_falls_back_to_auto(tmp_path):
    """An out-of-vocabulary mode is not a mode."""
    plan_dir = tmp_path / "plan-bogus-mode"
    plan_dir.mkdir()
    (plan_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps({"version": 1, "mode": "yolo", "timeout_sec": 60, "hooks": []}),
        encoding="utf-8",
    )

    assert load_for_plan(plan_dir)["mode"] == "auto"


# ---------------------------------------------------------------------------
# No global default, no cross-plan inheritance
# ---------------------------------------------------------------------------


def test_no_global_config_candidate_is_read(tmp_path):
    """A file that looks like a global default is not a default.

    The "no global default" rule is only real if a plausible global
    file is *ignored*.  This one sits next to the plan, where a
    fallback chain would look for it, and declares ``strict``.
    """
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / "plan-1"
    plan_dir.mkdir(parents=True)
    (plans_root / HITL_CONFIG_FILENAME).write_text(
        json.dumps({"version": 1, "mode": "strict", "timeout_sec": 60, "hooks": []}),
        encoding="utf-8",
    )
    (plans_root / "global_hitl_config.json").write_text(
        json.dumps({"version": 1, "mode": "strict", "hooks": []}),
        encoding="utf-8",
    )

    assert load_for_plan(plan_dir) == {
        "version": 1,
        "mode": "auto",
        "timeout_sec": 1800,
        "hooks": [],
    }

    # A sibling plan's file is not inherited either.
    sibling = plans_root / "plan-2"
    sibling.mkdir()
    (sibling / HITL_CONFIG_FILENAME).write_text(
        json.dumps({"version": 1, "mode": "strict", "hooks": []}),
        encoding="utf-8",
    )

    assert load_for_plan(plan_dir)["mode"] == "auto"


# ---------------------------------------------------------------------------
# The hook element carries the fields its kind needs
# ---------------------------------------------------------------------------


def test_hook_element_fields_survive_the_read(tmp_path):
    """A reader that dropped ``url`` would leave an http hook with nowhere to POST.

    The contract gives each kind its own fields — ``destination`` for
    ``cc_connect`` / ``feishu`` / ``telegram``, ``url`` for ``http``,
    ``command`` for ``cmd`` — plus an optional per-hook
    ``timeout_sec``.  The reader reports what the plan stored; which
    entries are deliverable is the dispatcher's question, not this
    module's.
    """
    plan_dir = tmp_path / "plan-http"
    plan_dir.mkdir()
    (plan_dir / HITL_CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": 1800,
                "hooks": [
                    {
                        "kind": "http",
                        "url": "https://example.invalid/hook",
                        "timeout_sec": 5,
                    },
                    {
                        "kind": "cc_connect",
                        "destination": "grp-1",
                        "session_id": "s-1",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    hooks = load_for_plan(plan_dir)["hooks"]

    assert hooks[0] == {
        "kind": "http",
        "url": "https://example.invalid/hook",
        "timeout_sec": 5,
    }
    assert hooks[1] == {
        "kind": "cc_connect",
        "destination": "grp-1",
        "session_id": "s-1",
    }


# ---------------------------------------------------------------------------
# A write that fails is logged, not raised
# ---------------------------------------------------------------------------


def test_write_failure_is_logged_not_raised(tmp_path):
    """A plan whose hook configuration cannot be persisted is still a plan.

    The plan creation path must not fail because of this file: a plan
    with no hooks is a state the plan already knows how to be in (its
    decisions are simply unroutable), while a failed plan creation is
    a plan that does not exist.

    Two failures are covered, because they are two different code
    paths: the plan directory cannot be created, and the file itself
    cannot be written.
    """
    # The parent of the plan directory is a regular file, so the
    # directory cannot be created.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    written = write_for_plan(
        blocker / "plan", {"source": "plan_creation", "destination": "grp-1"}
    )

    # The origin names no session, so there is nobody to ask and the
    # hook list is empty — a plan with no hooks is a state a plan
    # already knows how to be in.
    assert written["hooks"] == []

    # The plan directory exists, but the config path is occupied by a
    # directory, so the write itself cannot happen.
    occupied = tmp_path / "plan-occupied"
    occupied.mkdir()
    (occupied / HITL_CONFIG_FILENAME).mkdir()

    written = write_for_plan(
        occupied, {"source": "plan_creation", "destination": "grp-1"}
    )

    assert written["hooks"] == []
