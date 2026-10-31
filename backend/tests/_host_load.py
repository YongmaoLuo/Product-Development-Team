"""One shared gate for "is this host too busy to measure a wall-clock budget?".

Why the gate exists
-------------------
The performance and time-sensitive lanes assert on wall-clock bounds.  A
bound the code under test really honours can still fail when the host is
busy, because the work did not get the CPU.  That failure is
indistinguishable from a regression at a glance, and telling the two apart
costs more than the failure did.

So the load average is checked *before* the bound is asserted, and a busy
host skips the case instead of failing it.  The reason travels with the
skip, so the lane log records why the bound was not asserted rather than
leaving a gap that reads like a pass.

Why it is one module
--------------------
The decision was already being made, but inline, once per file, and the
copies had drifted.  One gate means one limit and one message format.

Why every input is a parameter
------------------------------
A gate whose only input is the machine's load average can only be
exercised on a host that happens to be busy -- and a test that can only
fail when the machine is busy is not a test.  The load, the limit and the
environment are therefore all injectable, and ``None`` means "read the
real thing".

Standard library only, and deliberately no ``pytest`` import: the gate
has to be usable from ``conftest.py`` as well as from ordinary test
modules, and a conftest is imported before the plugin machinery that
provides ``pytest.skip`` is in place.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional

#: The environment variable that overrides the load limit.
HOST_LOAD_LIMIT_ENV = "PDT_HOST_LOAD_LIMIT"

#: The load limit used when the environment does not give a usable one.
HOST_LOAD_LIMIT_DEFAULT = 20

#: The environment variable a CI runner sets to say "this is a CI run".
CI_ENV = "CI"

#: The spellings of ``CI`` that mean "yes, this is a CI run".
_CI_TRUE_VALUES = frozenset({"1", "true", "yes"})


def _source(env: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    """The environment to read: the injected one, or the process's own."""
    return os.environ if env is None else env


def host_load_limit(env: Optional[Mapping[str, str]] = None) -> float:
    """The load average above which this host counts as too busy to measure.

    ``env`` defaults to the process environment.  A limit that is not a
    number, or that is not positive, falls back to the default: a broken
    override must not silently turn the gate off, and a zero or negative
    limit would skip every case -- including the ones CI is waiting for.
    """
    raw = _source(env).get(HOST_LOAD_LIMIT_ENV)
    if raw is None:
        return float(HOST_LOAD_LIMIT_DEFAULT)
    try:
        limit = float(raw)
    except (TypeError, ValueError):
        return float(HOST_LOAD_LIMIT_DEFAULT)
    if limit <= 0:
        return float(HOST_LOAD_LIMIT_DEFAULT)
    return limit


def current_host_load() -> float:
    """This host's one-minute load average, or ``0.0`` where there is none.

    A platform that cannot report a load average reports ``0.0``, not a
    failure: "we cannot tell" is not "the host is busy", so an
    unsupported platform must not skip anything.
    """
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError, NotImplementedError):
        return 0.0


def ci_requested(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this run is a CI run.

    A CI runner's load is not the code's own doing -- the runner is
    shared with whatever else the host is busy with -- so the gate stands
    down there.  A lane that quietly skipped on CI would report green
    while asserting nothing at all.
    """
    raw = _source(env).get(CI_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() in _CI_TRUE_VALUES


def skip_reason(
    *,
    load: Optional[float] = None,
    limit: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Why this case should be skipped for host load, or ``None``.

    ``load`` defaults to the host's current load average, ``limit`` to the
    configured limit, and ``env`` to the process environment.  The message
    carries both numbers, so the lane log says why the bound was not
    asserted without anybody having to go and reproduce the load.
    """
    if ci_requested(env):
        return None
    if load is None:
        load = current_host_load()
    if limit is None:
        limit = host_load_limit(env)
    if load >= limit:
        return f"skipped: host load {load:.1f} >= {limit:g}"
    return None