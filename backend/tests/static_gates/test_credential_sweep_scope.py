"""Static gate: the credential sweep stays inside the scope it was given.

Why this gate exists
--------------------
``utils.secret_sweep`` is a **machine-wide** rewrite: it walks the
machine's real temp roots and overwrites the credentials it finds in the
files there.  ``backend/service_manager.py`` now hands every service
child it starts an environment carrying ``PDT_DISABLE_TEMP_SWEEP=1``, so a
declared service -- a one-shot fixture for one verification round -- does
not pay for that walk before it answers its readiness probe.

That fix is one line at one spawn site.  The remaining risk is not that
the fix is wrong; it is that a later change widens it while looking
harmless.  Four ways of widening it are worth pinning, because each of
them is a decision the plan made explicitly:

1. **No second switch.**  ``PDT_CREDENTIAL_SWEEP`` was considered and
   rejected.  The application must keep exactly one sweep switch, so a
   later change cannot start reading a key that nothing writes -- and
   cannot start *writing* one either, which would give the same
   behaviour two names and two sources of truth.
2. **"Set means off" survives.**  ``sweep_default_roots()`` must return
   ``([], 0, [])`` and touch nothing while the switch holds any
   non-empty value -- including ``"0"`` and ``"false"``.  A reading that
   treated ``"0"`` as "on" would put the machine-wide rewrite back on
   the path the fix removed, and ``"0"`` is a value a caller really
   would write.
3. **The request-guard self-check window stays at 60 seconds.**  The
   fix's premise is that the window is longer than the walk it removes.
   Shortening the window re-creates the failure the fix addresses, and
   it would do so silently: a shorter window still refuses an unguarded
   instance, it just refuses one that was still booting.
4. **Both boot sweeps stay in ``_run_boot_sweeps()``.**  The orphan
   ledger sweep and the credential sweep are two different jobs.  Dropping
   either one is not a simplification, it is a hole: the ledger sweep is
   what releases the port a killed round left held, and the credential
   sweep is what collects the payload that round left behind.

What this gate does not do
--------------------------
It reads the application source and exercises the sweep module's own
entry point.  It starts no process, spawns no server and touches no temp
root: the sweep is exercised through the path that must do nothing.

The source is read through :mod:`tests.app_source` rather than through
``server.py``, because two of the four constraints live in modules that
are not ``server.py`` at all.  A scan that looked in the wrong place
would find nothing, and finding nothing passes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

#: ``backend/`` -- this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import app_source as app_source_module  # noqa: E402
from utils import secret_sweep as secret_sweep_module  # noqa: E402


# ---------------------------------------------------------------------------
# What the gate is about
# ---------------------------------------------------------------------------

#: The switch that disables the machine-wide credential sweep.  There is
#: exactly one, and this is its name.
SWITCH = "PDT_DISABLE_TEMP_SWEEP"

#: The name that was considered and rejected.  Its absence is the
#: evidence that the application still has one switch and not two.
REJECTED_SWITCH = "PDT_CREDENTIAL_SWEEP"

#: How long the request-guard self-check waits for the instance to come
#: up and answer.  The fix's premise is that this is longer than the
#: walk it removes.
SELFCHECK_WINDOW_SECONDS = 60.0

#: The request-guard self-check's own socket timeout -- a different
#: number from the window above, and pinned separately so that "shorten
#: the wait" cannot be reached by editing the probe instead.
SELFCHECK_PROBE_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Reading a default value out of the source
# ---------------------------------------------------------------------------


def _timeout_default(function_name: str) -> object:
    """The default value of *function_name*'s ``timeout`` parameter.

    Read from the AST rather than by importing the module: importing
    ``server`` to look at a default value is a side effect the gate does
    not need, and the number is a literal in the source either way.

    Positional defaults align with the **tail** of the positional
    arguments, so the default of ``timeout`` is the one whose distance
    from the end of the argument list matches.
    """
    node = app_source_module.find_def(function_name).node
    args = node.args
    names = [a.arg for a in args.args]
    assert "timeout" in names, (
        f"{function_name}() has no 'timeout' parameter "
        f"(it has {names!r}); the constraint this gate pins cannot be read "
        f"from it any more."
    )
    index = names.index("timeout")
    offset = len(args.args) - len(args.defaults)
    return ast.literal_eval(args.defaults[index - offset])


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_second_switch_introduced() -> None:
    """The application has one sweep switch, and it is the existing one.

    A second switch is a second source of truth.  The two would be read
    by different modules, and the disagreement between them would be
    silent: each module's own tests stay green while one of them decides
    the machine-wide rewrite is off and the other decides it is on.
    """
    src = app_source_module.app_source()

    assert SWITCH in src, (
        f"{SWITCH!r} does not appear in the application source. The gate "
        f"is scanning the wrong tree -- a scan that matches nothing "
        f"passes, and that is not evidence of anything."
    )
    assert REJECTED_SWITCH not in src, (
        f"{REJECTED_SWITCH!r} appears in the application source. There is "
        f"one sweep switch ({SWITCH!r}); a second name for the same "
        f"behaviour is a second source of truth, and the two will drift."
    )


def test_secret_sweep_disable_env_unchanged() -> None:
    """The sweep still reads the switch it has always read.

    The name is the contract between the spawn site that sets it and the
    sweep that obeys it.  Renaming either side without the other leaves
    a switch that is set and never read -- which reads as "the sweep is
    off" and is in fact "the sweep runs".
    """
    assert secret_sweep_module.DISABLE_ENV == SWITCH, (
        f"utils.secret_sweep.DISABLE_ENV is "
        f"{secret_sweep_module.DISABLE_ENV!r}; the spawn site sets "
        f"{SWITCH!r}. The two must be the same string."
    )


@pytest.mark.parametrize("value", ["1", "0", "false"])
def test_sweep_default_roots_noop_when_disabled(monkeypatch, value: str) -> None:
    """Any non-empty value of the switch disables the sweep entirely.

    The switch is a presence flag, not a boolean: ``"0"`` and ``"false"``
    are values a caller really would write, and reading them as "on"
    would put the machine-wide rewrite back on the path the fix removed.

    The traversal functions are replaced by ones that raise, so "the
    sweep did nothing" is checked by the sweep not reaching them -- not
    by a count of what it would have done.
    """
    monkeypatch.setenv(secret_sweep_module.DISABLE_ENV, value)

    def _must_not_run(*args, **kwargs):
        raise AssertionError(
            "the sweep ran with "
            f"{secret_sweep_module.DISABLE_ENV}={value!r} set; the switch "
            "is a presence flag and any non-empty value disables the walk"
        )

    monkeypatch.setattr(secret_sweep_module, "default_roots", _must_not_run)
    monkeypatch.setattr(secret_sweep_module, "sweep", _must_not_run)
    monkeypatch.setattr(secret_sweep_module, "prune_residue", _must_not_run)

    assert secret_sweep_module.sweep_default_roots(apply=True) == ([], 0, []), (
        f"with {secret_sweep_module.DISABLE_ENV}={value!r} set, "
        "sweep_default_roots() must return ([], 0, []) -- no findings, no "
        "failed redactions, no pruned directories"
    )


def test_request_guard_selfcheck_window_is_sixty_seconds() -> None:
    """The request-guard self-check window is still 60 seconds.

    The window is how long the guard waits for the instance to come up
    and answer the probe.  It is the number the fix's premise rests on:
    the walk the fix removes is longer than the window, so a service
    child that sweeps loses the round.  Shortening the window would put
    that failure back, and it would do so quietly -- a shorter window
    still refuses an unguarded instance, it just refuses one that was
    still booting.

    Two numbers are pinned here, because they are two different waits:
    the window itself, and the probe's own socket timeout.

    The window lives in ``_start_request_guard_selfcheck()`` -- it is the
    deadline the self-check thread waits for the instance to come up --
    and the probe's socket timeout lives in
    ``_request_guard_selfcheck_on_port()``.  They are different waits for
    different things, and only the first one is the window.
    """
    window = _timeout_default("_start_request_guard_selfcheck")
    assert window == SELFCHECK_WINDOW_SECONDS, (
        f"the request-guard self-check window is {window!r}; it must stay "
        f"at {SELFCHECK_WINDOW_SECONDS!r}. A shorter window loses the "
        "race the sweep switch exists to win."
    )

    probe_timeout = _timeout_default("_request_guard_selfcheck_on_port")
    assert probe_timeout == SELFCHECK_PROBE_TIMEOUT_SECONDS, (
        f"the request-guard self-check probe's timeout is {probe_timeout!r}; "
        f"it must stay at {SELFCHECK_PROBE_TIMEOUT_SECONDS!r}. It is a "
        "different wait from the window above, and shortening it is the "
        "same failure by another route."
    )


def test_boot_sweeps_still_reap_orphan_ledger() -> None:
    """``_run_boot_sweeps()`` still runs both of its sweeps.

    The orphan ledger sweep is not the credential sweep, and it is not
    redundant with it: the ledger records the services a round declared
    and never reaped, and only the ledger sweep can release the port a
    killed round left held.  A change that keeps the credential sweep and
    drops the ledger sweep looks like a simplification and leaves every
    orphan service running.
    """
    body = app_source_module.find_def("_run_boot_sweeps").text

    assert "_reap_all_plans(" in body, (
        "_run_boot_sweeps() no longer calls the orphan ledger sweep. The "
        "services a killed round declared stay running and hold their "
        "ports."
    )
    assert "_sweep_credential_residue(apply=True)" in body, (
        "_run_boot_sweeps() no longer calls the credential sweep. The "
        "payload a killed round left behind keeps its live credential."
    )