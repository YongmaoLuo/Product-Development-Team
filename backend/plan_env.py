"""The two truths a plan's context is made of.

A plan's context used to be four environment variables: the plan's id,
the root the plan's directory hangs under, the plan's own directory,
and the verification sub-agent's copy of that same directory.  Three of
the four were the same fact.  The plan's own directory is
``<plans root>/<plan id>`` by construction, and the verification
sub-agent's copy was written with the same value as the write scope the
hooks read -- so its settings carried the plan directory twice, under
two names, with nothing in the shape of the values that could tell them
apart.

A copy of a fact is a cache, and a cache drifts.  The executor wrote
the plan's directory for the tree it was running; the coding agent
wrote the plans root for the tree *it* resolved; the two answers could
name different directories.  When they did, one plan had two ledgers --
the backend's sweep cannot see the sub-agents, so the plan's live set
never reached zero -- and the operator's startup sweep found directories
the suite had created and pushed cards for them.

So there are two truths and one derivation.  The truths are the plan's
id and the *absolute* root its directory hangs under; the plan's own
directory is derived from those two by :func:`plan_dir_for`, by
everyone who needs it, from the same two values.  There is no third
copy to drift, and no way to write the plan's directory into the
environment without also writing the two facts it was derived from.

Only the standard library is imported here.  This module is the one
producer of the plan context, and it is imported by the executor, by
the coding tool, by the sub-agent registry and by the test harness --
so it must not import anything that imports them back.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: The plan's id.  The one fact that says *which* plan this is.
PLAN_ID_ENV = "PDT_PLAN_ID"

#: The root the plan's directory hangs under.  Always absolute: the
#: executor's working directory is ``backend/``, so a relative root
#: names one directory for the backend and another for everything the
#: backend starts.
PLANS_DIR_ENV = "PDT_PLANS_DIR"

__all__ = [
    "PLAN_ID_ENV",
    "PLANS_DIR_ENV",
    "plan_dir_for",
    "plan_env",
    "plan_id",
    "plans_dir",
]


def plan_env(plan_id, plans_dir) -> dict[str, str]:
    """The two truths, as one environment update.

    ``plan_id`` is the plan's id; an empty one is *no* plan and leaves
    the key out entirely.  ``plans_dir`` is the root the plan's
    directory hangs under, and it is resolved to an absolute path here:
    the two truths are handed to processes whose working directory is
    not the one that produced them, so a relative root would name a
    different tree on the other side.

    The plan's own directory is deliberately NOT among the keys.  It is
    ``<plans_dir>/<plan_id>`` -- a derivation, not a fact -- and a
    derivation that is also stored is a second copy, and the second copy
    is what drifts (one plan, two ledgers).  :func:`plan_dir_for` is
    the one way to name it.
    """
    env: dict[str, str] = {}
    if plan_id:
        env[PLAN_ID_ENV] = str(plan_id)
    if plans_dir:
        env[PLANS_DIR_ENV] = str(Path(plans_dir).expanduser().resolve())
    return env


def plan_id() -> Optional[str]:
    """The plan's id, as the environment names it.

    An empty value is no plan: ``None``.  There is no default and no
    fallback -- a process that was not told which plan it works for
    says so by not having the variable at all.
    """
    raw = os.environ.get(PLAN_ID_ENV)
    if not raw:
        return None
    return raw


def plans_dir() -> Optional[Path]:
    """The plans root, as the environment names it.

    A **relative** value is refused and answered with ``None``.  The
    root is read by the backend *and* handed to the processes it
    starts, and those processes do not share a working directory with
    it -- the executor's is ``backend/``.  A relative root would
    therefore name one directory for the backend and another for
    everything below it, which is the same drift the derived variables
    used to cause.  Absolute means absolute: the caller can trust the
    value it gets back.
    """
    raw = os.environ.get(PLANS_DIR_ENV)
    if not raw:
        return None
    try:
        candidate = Path(raw).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not candidate.is_absolute():
        return None
    return candidate


#: Private aliases.  :func:`plan_dir_for` takes parameters named after
#: the two accessors above, and a parameter shadows the name it is
#: given -- so the accessors are reached through these aliases.
_env_plan_id = plan_id
_env_plans_dir = plans_dir


def plan_dir_for(plan_id=None, plans_dir=None) -> Optional[Path]:
    """The plan's own directory, derived from the two truths.

    ``<plans_dir>/<plan_id>``, and ``None`` when either half is
    missing.  The halves default to the environment; a half given
    explicitly overrides it.

    No existence check is made here.  Whether a directory that the two
    truths name has to *exist* is the caller's own semantics: the
    sub-agent registry treats a plan directory that is not there as "no
    ledger for this plan", while other callers treat it as a directory
    to create.  Deciding that here would take the decision away from
    both.
    """
    pid = plan_id if plan_id is not None else _env_plan_id()
    root = plans_dir if plans_dir is not None else _env_plans_dir()
    if not pid or root is None:
        return None
    try:
        root_path = Path(root).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not root_path.is_absolute():
        return None
    return root_path / str(pid)
