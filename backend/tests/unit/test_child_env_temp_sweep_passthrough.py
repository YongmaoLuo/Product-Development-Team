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
  decision away from whoever set it;
* the environment **production** hands to a spawned service carries the
  provider mode, and the mode is ``inherit`` — see the section below.

The other spawn sites do not need a rule of their own:
``test_server_signal_lifecycle.py`` copies ``os.environ`` wholesale (so
it inherits the key by construction), and the FakeProcess-based tests
never start a process at all.

The provider mode a spawned service is started in
--------------------------------------------------
The third rule is about the **provider mode**, and it belongs to the
same builder: the environment a service child is started with is
``service_manager._child_env``, and production's own builder has the
last word on it (the manual spawn harness calls that builder instead of
keeping a copy of it — a copy is a second shape, and the second shape
drifts).

The mode names the CC Switch instance on *this* box.  A service started
on a box without that instance dies on the way up:
``server.load_providers`` asks CC Switch for the provider chain, gets
``api_unreachable``, and the startup aborts — that contract is explicit:
there is no silent YAML fallback and no second source to answer
instead.  The child then never listens, and the manager waits out its
whole readiness budget before it can say so.

So the spawned child is started in ``inherit`` mode, and it is started
there **explicitly**: "unset" is not the same answer — unset means "ask
the machine", and the machine may not have a CC Switch at all.  Every
other spawn site in this repo already says ``inherit`` explicitly for
exactly this reason; the production spawn path was the one that did not,
and it is the one that decides whether a verification round can start a
server at all.

A declaration that does want a fleet says so in its own start command
(``export PDT_PROVIDER_MODE=cc-switch``): the variable is set before the
command runs, so the command's own export wins.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Any, Dict

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from tests.api.test_vp_bucket_progress_e2e import _child_env as bucket_child_env
from tests.integration.test_verification_progress_live import _child_env
from tests.test_server_json_cleanup import _child_env as cleanup_child_env

#: The key ``utils.secret_sweep`` reads (``secret_sweep.DISABLE_ENV``).
#: A non-empty value makes ``sweep_default_roots`` a no-op.
SWEEP_KEY = "PDT_DISABLE_TEMP_SWEEP"

#: The variable that pins the provider mode; see
#: ``coding_tool._detect_inherit_mode`` for what the two modes mean.
PROVIDER_MODE_KEY = "PDT_PROVIDER_MODE"

#: The mode a spawned service is started in.
INHERIT_MODE = "inherit"


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


# ---------------------------------------------------------------------------
# The provider mode a spawned service is started in
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeProc:
    """A child that was never really started.

    ``start_service`` polls the process once after the readiness wait;
    a child that is still running is the boring case, and what this pin
    is about is the environment, not the readiness verdict.
    """

    pid = 999999

    def poll(self) -> None:
        return None


def _spawned_environment(monkeypatch, tmp_path) -> Dict[str, str]:
    """The environment production hands to a spawned service.

    ``Popen`` is replaced, so nothing is really started, and the
    readiness probe is stubbed so it does not decide the outcome.
    """
    captured: Dict[str, Any] = {}

    def _fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr(sm.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sm, "wait_until_ready", lambda decl, t=None: True)
    # The child never existed, so there is nothing to wait for.
    monkeypatch.setattr(
        sm, "_await_exit", lambda proc, timeout=2.0: (False, None)
    )

    decl = ServiceDeclaration(
        name="child-env",
        port=_free_port(),
        start_cmd="npx next dev -p 3000",
        cwd=".",
    )
    sm.start_service(decl, tmp_path)
    assert captured.get("env"), "the service was never started"
    return captured["env"]


def test_a_spawned_service_is_started_in_inherit_mode(tmp_path, monkeypatch):
    """The spawned child's own provider mode is ``inherit``.

    The manager's mode is a statement about the **manager's** machine:
    it names the CC Switch instance the manager itself answers to.  A
    service is not the manager, and it is started on a box that may not
    have that instance at all -- where ``server.load_providers`` aborts
    the startup on ``api_unreachable`` and the child never listens.

    So the child is told to inherit, explicitly: "unset" would mean
    "ask the machine", and the machine here may not have a CC Switch.
    """
    monkeypatch.setenv(PROVIDER_MODE_KEY, "cc-switch")

    env = _spawned_environment(monkeypatch, tmp_path)

    assert env.get(PROVIDER_MODE_KEY) == INHERIT_MODE, (
        "the spawned service was handed the manager's provider mode "
        "({0!r}); a service is not the manager, and the manager's "
        "provider routing is not an answer for it".format(
            env.get(PROVIDER_MODE_KEY)
        )
    )
