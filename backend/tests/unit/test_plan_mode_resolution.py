"""A plan's ``mode`` resolves to one of two values -- or it is logged.

A plan may declare that it runs in ``test`` mode: the pipeline is
exercised, but the run must not touch anything real.  A plan that says
nothing, or says something nobody recognises, must still be runnable.
So the mode is never an error -- it is either one of the two known
values, or ``production`` plus a warning that says what was ignored.

The properties pinned here:

* a **missing** mode (no key, ``None``, an empty string) resolves to
  ``production`` and warns with ``plan_mode_missing``;
* an **unrecognised** mode resolves to ``production`` and warns with
  ``plan_mode_invalid=`` -- naming the value that was ignored;
* a **valid** mode resolves to itself after ``strip()`` / ``lower()`` and
  warns about nothing;
* the environment dictionary carries the **resolved** value, so an
  invalid mode never reaches a subprocess as-is.

The module under test is imported inside the test bodies so that a
missing module shows up as four failing tests rather than one
collection error.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def test_missing_mode_is_production_and_warns(caplog):
    from plan_mode import MODE_PRODUCTION, mode_from_flags

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert mode_from_flags({}) == (MODE_PRODUCTION, True)

    assert "plan_mode_missing" in caplog.text
    assert "fallback=production" in caplog.text


def test_invalid_mode_falls_back_and_warns(caplog):
    from plan_mode import MODE_PRODUCTION, resolve_mode

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert resolve_mode("prod") == (MODE_PRODUCTION, True)

    assert "plan_mode_invalid=" in caplog.text
    assert "prod" in caplog.text


def test_valid_modes_roundtrip(caplog):
    from plan_mode import MODE_PRODUCTION, MODE_TEST, mode_from_flags, resolve_mode

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert resolve_mode("test") == (MODE_TEST, False)
        assert resolve_mode("Production") == (MODE_PRODUCTION, False)
        assert resolve_mode(" Test ") == (MODE_TEST, False)
        assert mode_from_flags({"mode": "test"}) == (MODE_TEST, False)

    assert caplog.records == []


def test_plan_mode_env_uses_resolved_value():
    from plan_mode import PLAN_MODE_ENV, plan_mode_env

    env = plan_mode_env("bogus")

    assert env == {PLAN_MODE_ENV: "production"}
    assert "bogus" not in str(env)
    assert plan_mode_env("test") == {"PDT_PLAN_MODE": "test"}