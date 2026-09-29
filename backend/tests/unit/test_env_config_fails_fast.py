"""Missing configuration must fail loudly and immediately.

2026-09-23. This pins the half of the CI environment story that the
`.env.ci` template does *not* cover.

`tests/conftest.py::_env_file_for_tests` gives the suite the three
placeholders `env_config.REQUIRED_VARS` demands, so a fresh checkout can
run. That is a convenience for missing *test* configuration — it must not
turn a missing *deployment* variable into silence. The contract is:

  * a required variable that is absent raises ``RuntimeError`` naming it,
  * it raises **promptly** — no retry loop, no backoff, no poll — and
  * it never blocks, because a configuration error that hangs is far worse
    than one that exits: the process holds its resources and reports
    nothing.

Why the promptness assertion is worth a test rather than a comment: this
whole investigation started from a CI job that stopped responding with no
log, no step timeout and no job timeout. Whatever the cause turns out to
be, "configuration is missing" is exactly the class of condition that
should never be allowed to behave that way.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from env_config import (  # noqa: E402
    REQUIRED_VARS,
    load_env,
    load_env_config,
    validate_config,
)

#: The committed, non-secret template that conftest materialises.
TEMPLATE = BACKEND_DIR / ".env.ci"

#: An env path that does not exist, so ``load_dotenv`` is skipped entirely
#: and the result depends only on ``os.environ``. Without this the loader
#: would re-read the ``.env`` conftest just wrote and the test would be
#: asserting against the fixture instead of against the loader.
NO_ENV_FILE = "/nonexistent/definitely-not-a-dotenv-file"


def _parse_template(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Fail fast, do not hang
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_a_missing_required_var_raises_and_names_itself(monkeypatch, missing):
    """Isolate one variable at a time.

    The loader reports the first absent entry in ``REQUIRED_VARS`` order,
    so deleting all three would only ever exercise the first — and would
    leave the other two unable to fail this test no matter what happened
    to them.
    """
    for key in REQUIRED_VARS:
        if key == missing:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, "present")

    with pytest.raises(RuntimeError) as excinfo:
        load_env(NO_ENV_FILE)

    assert missing in str(excinfo.value), (
        f"the error must name the missing variable so an operator can act "
        f"on it; got {excinfo.value}"
    )


def test_it_raises_promptly_rather_than_retrying(monkeypatch):
    """A missing variable is not a transient condition.

    There is nothing to wait for, so there must be no waiting: no retry
    loop, no backoff, no provider-style poll. A configuration error that
    hangs holds the process, its file descriptors and its locks while
    reporting nothing — the failure mode this test exists to forbid.
    """
    for key in REQUIRED_VARS:
        monkeypatch.delenv(key, raising=False)

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        load_env(NO_ENV_FILE)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"load_env() took {elapsed:.2f}s to report a missing variable; it "
        f"must raise immediately"
    )


def test_the_lower_level_loader_raises_keyerror_not_a_hang(monkeypatch):
    """``load_env`` is the loud wrapper; ``load_env_config`` is the quiet
    one and must still terminate rather than block."""
    for key in REQUIRED_VARS:
        monkeypatch.delenv(key, raising=False)

    started = time.monotonic()
    with pytest.raises(KeyError):
        load_env_config(NO_ENV_FILE)
    assert time.monotonic() - started < 1.0


def test_an_absent_env_file_is_not_itself_an_error(monkeypatch, tmp_path):
    """The file is optional; only the variables are required.

    A machine that exports the three variables (a container, a CI job with
    ``env:``) must work without any ``.env`` at all.
    """
    for key in REQUIRED_VARS:
        monkeypatch.setenv(key, "from-the-environment")

    config = load_env_config(NO_ENV_FILE)

    for key in REQUIRED_VARS:
        assert config[key] == "from-the-environment"


# ---------------------------------------------------------------------------
# The committed template
# ---------------------------------------------------------------------------


class TestTheCommittedTemplate:
    def test_it_exists(self):
        assert TEMPLATE.exists(), (
            f"{TEMPLATE.name} is what conftest materialises on a machine "
            f"with no backend/.env; without it a fresh checkout cannot run "
            f"the suite"
        )

    def test_it_satisfies_the_loader(self, monkeypatch):
        for key in REQUIRED_VARS:
            monkeypatch.delenv(key, raising=False)
        for key, value in _parse_template(
            TEMPLATE.read_text(encoding="utf-8")
        ).items():
            monkeypatch.setenv(key, value)

        config = load_env(str(TEMPLATE))

        for key in REQUIRED_VARS:
            assert config.get(key), f"{key} is empty in the template"

    def test_it_passes_key_validation(self, monkeypatch):
        """``validate_config`` requires the ``sk-ant-`` prefix. A template
        that fails it would move the failure one step later instead of
        removing it."""
        for key in REQUIRED_VARS:
            monkeypatch.delenv(key, raising=False)
        for key, value in _parse_template(
            TEMPLATE.read_text(encoding="utf-8")
        ).items():
            monkeypatch.setenv(key, value)

        validate_config(load_env(str(TEMPLATE)))

    @pytest.mark.parametrize("key", REQUIRED_VARS)
    def test_every_required_value_is_obviously_a_placeholder(self, key):
        """A tracked file is a published file.

        These values are read by every CI run and sit in the repository
        history forever, so a real credential pasted here is a leak that
        deleting the line does not undo. Require each one to say so.
        """
        values = _parse_template(TEMPLATE.read_text(encoding="utf-8"))
        assert key in values, f"{key} must appear in {TEMPLATE.name}"

        value = values[key].lower()
        assert (
            "placeholder" in value
            or value.startswith("ci-")
            or "not-a-real" in value
        ), (
            f"{TEMPLATE.name} is committed and world-readable; {key} must "
            f"declare itself a placeholder, got {values[key]!r}"
        )
