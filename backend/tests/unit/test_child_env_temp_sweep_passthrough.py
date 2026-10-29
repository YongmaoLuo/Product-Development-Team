"""A spawned server must inherit the temp-sweep switch (2026-10-29).

Background
----------
``_pytest_hermetic_env.install_hermetic_env()`` sets
``PDT_DISABLE_TEMP_SWEEP=1`` for the **pytest process**, so the
machine-wide credential sweep never fires in-process.  That setting is
process-local: it reaches a child only through the child's environment.

Three test modules build the environment for a **spawned** server as a
*whitelist* — deliberately not a copy of the parent's environment, so
that ``PDT_SECRET_FD_*`` and ``HOME`` do not leak into the child.  A
whitelist has to be maintained by hand, and these three did not carry the
sweep switch: the child booted, ran the machine-wide sweep over the real
temp roots, and its lifespan took 18-21 s.  The live test then failed
with "did not become ready within 30.0s" — a timeout that looked like a
flaky port, not like a child that was busy rewriting the developer's own
residue.

What is pinned here
-------------------
* every ``_child_env`` that a real server is spawned with carries
  ``PDT_DISABLE_TEMP_SWEEP``;
* the value the parent already holds is passed through **unchanged** —
  including ``"0"``, which means "do sweep".  The child is entitled to
  disagree with the default, and a hard-coded ``"1"`` would take that
  decision away from whoever set it.

The other spawn sites do not need a rule of their own:
``test_server_signal_lifecycle.py`` copies ``os.environ`` wholesale (so
it inherits the key by construction), and the FakeProcess-based tests
never start a process at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from tests.api.test_vp_bucket_progress_e2e import _child_env as bucket_child_env
from tests.integration.test_verification_progress_live import _child_env
from tests.test_server_json_cleanup import _child_env as cleanup_child_env

#: The key ``utils.secret_sweep`` reads (``secret_sweep.DISABLE_ENV``).
#: A non-empty value makes ``sweep_default_roots`` a no-op.
SWEEP_KEY = "PDT_DISABLE_TEMP_SWEEP"


def _plans_root(tmp_path: Path) -> Path:
    return tmp_path / "plans"


def _state_db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_integration_child_env_disables_temp_sweep(tmp_path, monkeypatch):
    """The live-progress test's child env disables the sweep by default.

    The parent has no opinion here (the key is absent), so the child must
    be told to skip the machine-wide sweep — otherwise the spawned server
    spends its whole readiness budget walking the real temp roots.
    """
    monkeypatch.delenv(SWEEP_KEY, raising=False)

    env = _child_env(_plans_root(tmp_path), _state_db(tmp_path), 8124)

    assert SWEEP_KEY in env, (
        "the live-progress test spawns its server with a whitelisted env; "
        "without {0} the child runs the machine-wide credential sweep and "
        "the readiness wait times out".format(SWEEP_KEY)
    )
    assert env[SWEEP_KEY] == "1"


def test_bucket_child_env_disables_temp_sweep(tmp_path, monkeypatch):
    """Same contract for the bucket-progress e2e test's child env."""
    monkeypatch.delenv(SWEEP_KEY, raising=False)

    env = bucket_child_env(_plans_root(tmp_path), _state_db(tmp_path))

    assert SWEEP_KEY in env, (
        "the bucket-progress e2e test spawns its server with a whitelisted "
        "env; without {0} the child runs the machine-wide credential sweep".format(
            SWEEP_KEY
        )
    )
    assert env[SWEEP_KEY] == "1"


def test_json_cleanup_child_env_disables_temp_sweep(tmp_path, monkeypatch):
    """Same contract for the json-cleanup test's child env."""
    monkeypatch.delenv(SWEEP_KEY, raising=False)

    env = cleanup_child_env(_plans_root(tmp_path), _state_db(tmp_path))

    assert SWEEP_KEY in env, (
        "the json-cleanup test spawns its server with a whitelisted env; "
        "without {0} the child runs the machine-wide credential sweep".format(
            SWEEP_KEY
        )
    )
    assert env[SWEEP_KEY] == "1"


def test_explicit_value_is_passed_through(tmp_path, monkeypatch):
    """A value the parent already holds wins — ``"0"`` included.

    ``"0"`` is not an empty string, so the sweep still runs for a child
    that is told to.  The point is that the parent decides: a test that
    wants the sweep to fire says so, and the three ``_child_env`` do not
    overwrite it with the default.
    """
    monkeypatch.setenv(SWEEP_KEY, "0")

    envs: Dict[str, Dict[str, str]] = {
        "integration": _child_env(_plans_root(tmp_path), _state_db(tmp_path), 8124),
        "bucket": bucket_child_env(_plans_root(tmp_path), _state_db(tmp_path)),
        "cleanup": cleanup_child_env(_plans_root(tmp_path), _state_db(tmp_path)),
    }

    for name, env in envs.items():
        assert env.get(SWEEP_KEY) == "0", (
            "the {0} child env overwrote the parent's {1}={2!r} with {3!r}".format(
                name, SWEEP_KEY, "0", env.get(SWEEP_KEY)
            )
        )
