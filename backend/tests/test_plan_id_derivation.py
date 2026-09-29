"""Tests for :func:`task_manager.derive_plan_id_from_tasks_file`.

2026-09-08 plan — guards the canonical plan_id derivation in
``task_manager.TaskManager._persist_status_to_sqlite`` and
``agent.Agent.__init__``. Without these guards, a tasks file at
``plans/project/tasks.json`` silently produces ``plan_id='project'``
and pollutes ``state.db`` (the 22-task pollution that surfaced in
the project Feishu card on 2026-09-08).

Each test maps to one rule in :func:`derive_plan_id_from_tasks_file`:
  1. Empty parent name → ValueError
  2. Reserved / generic name → ValueError (case-insensitive)
  3. Too-short name → ValueError
  4. Forbidden characters → ValueError

A positive case (valid name → returned unchanged) anchors the
happy-path so future refactors don't accidentally widen the gate.
"""

from pathlib import Path

import pytest

from task_manager import derive_plan_id_from_tasks_file


# --- Rule 1: empty parent -------------------------------------------------


def test_empty_parent_name_raises(tmp_path):
    """`plans//tasks.json` style — parent.name is empty."""
    bad = tmp_path / ""  # actually creates a dir with empty name on POSIX; use a literal Path instead
    # Build a Path whose parent.name is empty by using a sentinel str
    fake = Path(str(tmp_path) + "/.anchor") / "tasks.json"
    # The .anchor dir name is non-empty though; force a literal empty parent
    p = Path("/tmp/foo") / "" / "tasks.json"
    # Pathlib collapses empty segments, so emulate by constructing a fake file:
    # the simplest portable test is to make parent.name evaluate to "" via
    # monkeypatched Path — but we can also use a Path with literal empty.
    # Use the trick: parent=Path('')
    bad_path = Path("tasks.json")  # parent.name == ''
    with pytest.raises(ValueError, match="Refusing to derive plan_id from empty"):
        derive_plan_id_from_tasks_file(bad_path)


# --- Rule 2: reserved names (case-insensitive) ----------------------------


@pytest.mark.parametrize(
    "name",
    [
        "project", "Project", "PROJECT", "projects",
        "tasks", "task", "plans", "plan",
        "tmp", "temp", "scratch",
        "test", "tests", "testing",
        "src", "app", "backend", "frontend",
        "root", "home", "workspace",
        "default", "untitled", "new", "demo",
    ],
)
def test_reserved_name_raises(tmp_path, name):
    """All reserved names must raise, regardless of case."""
    tasks_file = tmp_path / name / "tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("[]")
    with pytest.raises(ValueError, match="reserved"):
        derive_plan_id_from_tasks_file(tasks_file)


# --- Rule 3: too short ----------------------------------------------------


@pytest.mark.parametrize("name", ["a", "ab", "abc", "1", "x1", "_-_", "x.y"])
def test_too_short_name_raises(tmp_path, name):
    """Names < 4 chars must raise — prevents accidental 1/2/3 char ids."""
    tasks_file = tmp_path / name / "tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("[]")
    with pytest.raises(ValueError, match="too short"):
        derive_plan_id_from_tasks_file(tasks_file)


# --- Rule 4: forbidden characters -----------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # path separator / traversal attempts — these block SQL/CLI
        # manipulation regardless of the Unicode allowlist.
        "a/b/c", "back\\slash", "x../etc",
        # Shell metacharacters that enable command injection.
        # ``<>`` are excluded because they participate in shell
        # redirection; ``$;|`&`` would be parsed as control flow.
        "ab;cd", "ab&cd", "ab|cd", "ab$cd", "ab`cd",
        # newline enables command-line parsing attacks even though
        # other control chars (tab etc.) are allowed.
        "ab\ncd",
    ],
)
def test_forbidden_chars_raise(name):
    """Names with forbidden characters must raise (defence-in-depth).

    2026-09-09 (unicode letters fix): the previous allowlist
    ``[A-Za-z0-9._-]`` rejected Chinese / CJK / accented plan_ids that
    the backend server already
    accepted into ``plan_routing.plan_id`` SQLite tables. The new
    policy allows Unicode letters and most punctuation but still
    blocks path separators + the shell-metacharacter subset that
    enables command injection (``;|&$`\\n``).
    """
    # Use a sentinel parent dir whose name does NOT itself contain
    # forbidden chars; instead, monkeypatch the tasks_file so that
    # ``parent.name`` evaluates to the bad string. We can't easily
    # create a real directory named ``a;b`` on POSIX, so build a
    # fake Path and inject ``parent.name`` via subclass.
    class _FakePath(type(Path())):
        @property
        def name(self):
            return name

    fake_file = _FakePath("/dummy/tasks.json")
    with pytest.raises(ValueError, match="forbidden"):
        derive_plan_id_from_tasks_file(fake_file)


# --- Positive case --------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # Canonical production-style plan ids
        "20260823-production-plan",
        "20260101-baseline",
        "20260101-production-plan",
        # alphanumeric with allowed punctuation
        "plan.v2",
        "plan_v2",
        "abc-def_ghi.jkl",
        # 4-char minimum allowed (boundary)
        "abcd", "1234", "a.b1",
        # 2026-09-09 unicode-letters fix: Chinese / CJK plan_ids
        # already exist in state.db and must round-trip through the
        # validator without rejection.
        "20260101-支付网关重构-架构评审",
        "20260101-production-plan(consolidat",
        "20260101-网关重构与迁移计划：1)-",
        # accents / Latin-1 letters
        "café-noir-2026",
        "2026-naïve-plan",
    ],
)
def test_valid_name_returned_unchanged(tmp_path, name):
    """Valid names must be returned as-is (no normalization)."""
    tasks_file = tmp_path / name / "tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("[]")
    assert derive_plan_id_from_tasks_file(tasks_file) == name


# --- Regression test: the 2026-09-08 pollution case ----------------------


def test_2026_09_08_regression_plans_project_raises(tmp_path):
    """Regression: a tasks file at ``plans/project/tasks.json`` MUST
    raise. This is the exact layout that produced the 22-task pollution
    in state.db on 2026-09-08.
    """
    polluted = tmp_path / "project" / "tasks.json"
    polluted.parent.mkdir(parents=True, exist_ok=True)
    polluted.write_text("[]")
    with pytest.raises(ValueError, match="reserved"):
        derive_plan_id_from_tasks_file(polluted)
