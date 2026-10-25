"""HITL hooks — who gets asked about a plan's decisions.

What lives here
---------------
:mod:`hooks.config` holds the per-plan hook configuration: the file
that sits in a plan's directory and says whether anybody is asked when
that plan's execution hits a decision point, and how they are asked.

The file is a **plan property**.  Every plan carries its own, and the
configuration is read from that plan's own file or from nowhere: no
cross-plan inheritance, no global default, and no environment variable
consulted at read time.  Two plans that were created differently — one
from a Claude Code session, one by a ``curl`` call — have genuinely
different answers, and a global default would give both the same one.

One reader, one writer
----------------------
The two functions exported here are the only code in the tree that
reads or writes that file.  That is the point of the module rather
than an accident of its layout: a second implementation of these rules
would drift, and the drift would show up as a plan whose stored
configuration and read-back configuration disagree.  This module is a
re-export of that one implementation and contains no read or write
logic of its own.

The decision side of the vocabulary (``auto`` / ``strict``) is not
defined here — it is :class:`hitl.models.DecisionMode`, the same
closed vocabulary the decision table stores, so the mode configured for
a plan and the mode recorded with a decision cannot mean different
things.
"""

from __future__ import annotations

from .config import (
    DEFAULT_MODE,
    DEFAULT_TIMEOUT_SEC,
    HITL_CONFIG_FILENAME,
    HOOK_KIND_CC_CONNECT,
    SCHEMA_VERSION,
    config_path,
    default_config,
    load_for_plan,
    write_for_plan,
)

__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_SEC",
    "HITL_CONFIG_FILENAME",
    "HOOK_KIND_CC_CONNECT",
    "SCHEMA_VERSION",
    "config_path",
    "default_config",
    "load_for_plan",
    "write_for_plan",
]
