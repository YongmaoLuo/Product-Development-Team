"""The framework-fix lane's settings: the defaults, and the switch that
merges without asking.

Background
----------
The lane's capacity, its timeouts, its reminder interval and its merge
policy are one configuration surface, and the architecture's decision
table pins what that surface says when nobody has said anything
(architecture decision point 6).  Three rules are load-bearing:

* **A missing setting is not an error.**  A configuration file that
  names one setting and stays silent about the rest means "use the
  default for the rest" — not "refuse the file".  A lane that refuses
  to start because a file is half-written is worse than one that runs
  on the values the decision table pinned.

* **A value that cannot be used is refused.**  A capacity of zero is
  not a capacity, and a lane that accepted it would either admit
  nothing or divide by it.  The refusal is loud: the loader raises, so
  the mistake is found at the moment the file is written rather than
  the moment the lane misbehaves.

* **The merge switch is off unless somebody turned it on.**  This is the
  one setting whose wrong answer merges somebody else's work without
  asking them.  So the switch answers *true* only for the boolean
  ``true``: a missing key, an empty value, a number, a word — every one
  of them leaves the switch off, and a value that looks like an attempt
  to turn it on is warned about rather than obeyed.

TDD contract — 7 cases
----------------------
1. ``test_auto_merge_default_false`` —— 缺省 -> 关闭.
2. ``test_defaults_present`` —— 六个设置齐全，且是决策表钉住的值.
3. ``test_invalid_max_rejected`` —— ``max_active: 0`` -> 校验失败.
4. ``test_auto_merge_explicit_true`` —— 显式 ``true`` -> 开启.
5. ``test_missing_keys_use_defaults`` —— 缺键 -> 用默认值，不抛错.
6. ``test_auto_merge_other_spellings_stay_off`` —— 其它拼写 -> 关闭.
7. ``test_unknown_key_ignored_with_warning`` —— 未知键 -> 忽略并告警.

本文件另外覆盖的边界条件：``waiting_on_user_merge_hours``（未批复满多久升级）
是一个设置而不是写死的数 —— 生产读者 ``framework_fix_settings`` 把它交出来，
负值被拒绝。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ``config`` is imported under its bare name because that is the name the
# backend itself uses (``from config import AgentConfig`` in ``agent.py``,
# ``config_loader.py`` and ``refiner.py``).  Importing it as
# ``backend.config`` would make a *second* copy of the module, and the two
# copies would not agree about what a configuration object is.
from config import (  # noqa: E402
    DEFAULT_FRAMEWORK_FIX_CONFIG,
    ConfigError,
    framework_fix_config,
    load_config,
)

#: The seven settings the decision table pins, with the values it pins
#: them at.  ``auto_merge`` is not among them: it is a switch, and its
#: default is *off* rather than a number.
EXPECTED_DEFAULTS = {
    "max_active": 2,
    "max_per_repo": 2,
    "oversubscribe": 1,
    "repro_runs": 2,
    "merge_reminder_minutes": 30,
    "waiting_on_user_merge_hours": 2,
    "deadlock_minutes": 10,
}


def _write_config(directory: Path, body: str, name: str = "config.yaml") -> Path:
    """Write a configuration file and hand back its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. The merge switch is off when nobody turned it on
# ---------------------------------------------------------------------------


def test_auto_merge_default_false(tmp_path: Path) -> None:
    """A configuration that says nothing about ``auto_merge`` leaves it off.

    The switch is the one setting whose wrong answer merges somebody
    else's work without asking them, so "nobody said" has to mean "off"
    rather than "on, because the file did not object".

    Both shapes are checked: a file with a ``framework_fix`` section that
    names other settings, and a file with no such section at all.
    """
    path = _write_config(tmp_path, "framework_fix:\n  max_active: 3\n")
    assert load_config(path)["framework_fix"]["auto_merge"] is False

    bare = _write_config(tmp_path, "subsidiary_processes: []\n", name="bare.yaml")
    assert load_config(bare)["framework_fix"]["auto_merge"] is False


# ---------------------------------------------------------------------------
# 2. The defaults are the ones the decision table pinned
# ---------------------------------------------------------------------------


def test_defaults_present() -> None:
    """The six settings are there, at the values the decision table pinned.

    A capacity of two active fix plans, two per repository, one slot of
    oversubscription, two rounds per signature, a reminder every thirty
    minutes and a deadlock escalation after ten minutes.  These are the
    numbers the lane runs with when the file says nothing, so they are
    pinned here rather than left to the code that happens to read them.
    """
    settings = load_config()["framework_fix"]
    for name, value in EXPECTED_DEFAULTS.items():
        assert settings[name] == value, name


def test_defaults_present_without_a_section(tmp_path: Path) -> None:
    """A file with no ``framework_fix`` section still yields the defaults.

    The section is optional: the lane's settings live in one place, but
    the place is allowed to be empty.
    """
    path = _write_config(tmp_path, "subsidiary_processes: []\n")
    settings = load_config(path)["framework_fix"]
    for name, value in EXPECTED_DEFAULTS.items():
        assert settings[name] == value, name
    assert settings["auto_merge"] is False


# ---------------------------------------------------------------------------
# 3. A capacity that cannot be used is refused
# ---------------------------------------------------------------------------


def test_invalid_max_rejected(tmp_path: Path) -> None:
    """``max_active: 0`` fails the check instead of being used.

    Zero is not "no fix plans" — it is a capacity nothing can be
    admitted into, and a lane that admitted into it would stall every
    plan it was supposed to serve.  The refusal is loud: the loader
    raises, and the message names the setting that is wrong.
    """
    path = _write_config(tmp_path, "framework_fix:\n  max_active: 0\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "max_active" in str(excinfo.value)


def test_invalid_max_rejected_is_a_value_error(tmp_path: Path) -> None:
    """The refusal is a ``ValueError``, so a caller can catch one thing.

    ``ConfigError`` derives from ``ValueError``; a caller that only
    knows "this configuration is not usable" catches that.
    """
    path = _write_config(tmp_path, "framework_fix:\n  max_active: 0\n")
    with pytest.raises(ValueError):
        load_config(path)


# ---------------------------------------------------------------------------
# 4. The switch opens for an explicit ``true``
# ---------------------------------------------------------------------------


def test_auto_merge_explicit_true(tmp_path: Path) -> None:
    """``auto_merge: true`` turns the switch on.

    The boolean is the only spelling that opens it, and this is the one
    spelling that means the switch was turned on on purpose.
    """
    path = _write_config(tmp_path, "framework_fix:\n  auto_merge: true\n")
    assert load_config(path)["framework_fix"]["auto_merge"] is True


# ---------------------------------------------------------------------------
# 5. A missing setting takes its default
# ---------------------------------------------------------------------------


def test_missing_keys_use_defaults(tmp_path: Path) -> None:
    """Naming one setting leaves the others at their defaults.

    No error is raised: a file that names half of the section is a file
    an operator is still writing, and the lane has to run on the
    defaults for the rest.
    """
    path = _write_config(tmp_path, "framework_fix:\n  max_active: 5\n")
    settings = load_config(path)["framework_fix"]
    assert settings["max_active"] == 5
    for name, value in EXPECTED_DEFAULTS.items():
        if name == "max_active":
            continue
        assert settings[name] == value, name
    assert settings["auto_merge"] is False


def test_defaults_are_the_module_level_default() -> None:
    """The loader's defaults and the module's default table agree.

    Two tables that drift apart are one table too many: a reader that
    asks "what does this lane run with when nobody says?" would get two
    answers.
    """
    settings = framework_fix_config(None)
    assert settings == DEFAULT_FRAMEWORK_FIX_CONFIG
    for name, value in EXPECTED_DEFAULTS.items():
        assert DEFAULT_FRAMEWORK_FIX_CONFIG[name] == value, name


def test_framework_fix_settings_reader() -> None:
    """The production reader hands the lane its settings.

    The merge approval runs on two thresholds — how long a merge-ready
    fix waits before it is announced again, and how long it waits before
    it is escalated.  A threshold written into the call site is a
    threshold nobody can change, so the production code reads both of
    them here.

    The reader is cached, so a case that changes the configuration file
    empties the cache first — that is what ``cache_clear`` is for.
    """
    from config import framework_fix_settings

    framework_fix_settings.cache_clear()
    try:
        settings = framework_fix_settings()
    finally:
        framework_fix_settings.cache_clear()

    for name, value in EXPECTED_DEFAULTS.items():
        assert settings[name] == value, name
    assert settings["auto_merge"] is False


def test_waiting_on_user_merge_hours_is_a_setting(tmp_path: Path) -> None:
    """The escalation threshold is a number an operator can change.

    Two hours is the default the decision table pins; the point of the
    test is that the threshold is *read* from the configuration at all —
    a value nobody can name is a value nobody can change.
    """
    path = _write_config(
        tmp_path, "framework_fix:\n  waiting_on_user_merge_hours: 5\n"
    )
    settings = load_config(path)["framework_fix"]
    assert settings["waiting_on_user_merge_hours"] == 5


def test_waiting_on_user_merge_hours_refuses_a_negative_value(
    tmp_path: Path,
) -> None:
    """A negative wait is not a wait — the loader refuses it.

    The bound is zero: zero is a coherent answer ("escalate at once"),
    a negative one is not a duration at all.
    """
    path = _write_config(
        tmp_path, "framework_fix:\n  waiting_on_user_merge_hours: -1\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "waiting_on_user_merge_hours" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. No other spelling opens the switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ['"yes"', '"on"', '"1"', '""', '"true"', "1", "0"],
)
def test_auto_merge_other_spellings_stay_off(tmp_path: Path, spelling: str) -> None:
    """Only the boolean ``true`` opens the switch.

    A quoted ``"true"`` is a string, not the boolean, and a string that
    *looks* like the switch is exactly the kind of value a typo makes.
    The switch stays off, and the value that was refused is warned
    about rather than obeyed.
    """
    path = _write_config(
        tmp_path, f"framework_fix:\n  auto_merge: {spelling}\n", name="c.yaml"
    )
    assert load_config(path)["framework_fix"]["auto_merge"] is False


# ---------------------------------------------------------------------------
# 7. A setting nobody knows is ignored, and said out loud
# ---------------------------------------------------------------------------


def test_unknown_key_ignored_with_warning(tmp_path: Path, caplog) -> None:
    """A key the lane does not know is ignored — and warned about.

    Ignoring it silently is how a misspelled setting becomes a setting
    nobody notices is missing, so the loader says which key it dropped.
    """
    path = _write_config(
        tmp_path, "framework_fix:\n  max_active: 2\n  colour: blue\n"
    )
    with caplog.at_level(logging.WARNING):
        settings = load_config(path)["framework_fix"]

    assert "colour" not in settings
    assert settings["max_active"] == 2
    assert any(
        "colour" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
