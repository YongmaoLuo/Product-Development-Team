"""backend/plan_scheduler — the PDT-native scheduler.

The scheduler's settings and where they come from live in
:mod:`plan_scheduler.config`.  This package is the home the later
modules are added to — the worker thread, the command mailbox, the tick
pipeline — and it is deliberately importable on its own: reading the
configuration must not require the rest of the scheduler to exist.
"""
from __future__ import annotations

from . import config
from .config import SchedulerConfig, SchedulerConfigLoader

__all__ = ["SchedulerConfig", "SchedulerConfigLoader", "config"]