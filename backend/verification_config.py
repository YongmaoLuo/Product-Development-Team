"""
Verification Configuration
===========================

Centralises the VerificationAgent's per-method timeouts and parallelism
settings. The constants used to be hard-coded inside
``verification_agent._execute_*`` (300s per method, before the
2026-09-13 unification; kept here only as history)
ui_validation, 180s for code_review, 60s for api_test). This module
extracts them into a yaml-driven config so the numbers can be tuned
without touching code, and provides a pure-function :class:`TimeoutPolicy`
that performs the per-VP override resolution.

The module is intentionally dependency-light: it does not import
``verification_agent`` (which would create a circular import) and it
does not touch any LLM or filesystem-on-startup code. All file reads
are explicit via :meth:`TimeoutPolicy.from_yaml` / :meth:`TimeoutPolicy.from_dict`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional


# Hard-coded defaults — these are used when the yaml file is missing
# or fails to parse. They are intentionally identical to the numbers
# that were previously inlined in verification_agent.py so the
# behaviour does not change for anyone relying on the old hard-codes.
#
# 2026-09-08: raised the former ``automated_test`` from 120 → 3600 and
# ``api_test`` from 60 → 3600. (``automated_test`` itself was retired on
# 2026-09-18 — see ``verification_subagent.SUPPORTED_METHODS`` — so only
# the three surviving methods are keyed here.)
#
# 2026-09-13: the per-VP ``timeout_seconds``
# override was DELETED. These values are no longer per-call knobs:
# they are surface metadata only (``vp_start`` event observability)
# and always equal the single hard wall-clock cap,
# ``VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS=3600``. The
# actual enforcement layers are:
#   - 1-hour outer cap: flat, no per-VP scaling
#   - 15-min idle detector: ``coding_tool.DEFAULT_TOTAL_TIMEOUT=900``
DEFAULT_PER_METHOD_TIMEOUT_SECONDS: Dict[str, int] = {
    "ui_validation": 3600,
    "code_review": 3600,
    "api_test": 3600,
    # 2026-09-18（D5 follow-up）：Phase 2 的两个方法。``full_ci`` 跑的是
    # 整仓门禁（实跑量级在 20-30 分钟），但这里不是给它加预算 —— 上方
    # HARD_WALL_CLOCK_CAP_SECONDS 的"扁平一小时"契约对所有方法一视同仁，
    # 加一个更小的值只会让内层 wait_for 提前砍掉一条正在健康运行的门禁
    # （2026-09-08 那个 ``automated_test=120`` 的 bug 就是这么来的）。
    # 真正约束整仓命令时长的是 VP 自己的 ``ci_timeout_seconds``。
    "e2e": 3600,
    "full_ci": 3600,
}
DEFAULT_GLOBAL_TIMEOUT_SECONDS: int = 3600
DEFAULT_PARALLELISM_CAP: int = 4


class TimeoutPolicy:
    """Pure-function per-VP timeout resolver.

    2026-09-13: the per-VP ``timeout_seconds`` override branch was
    DELETED. Resolution is now method-level only:

    1. **Method-level default** — keyed by ``verification_method``
       (``ui_validation``, ``code_review``, ``api_test``, ``e2e``,
       ``full_ci``). Built from yaml, with hard-coded fallbacks when the
       yaml is missing.

    2. **Global default** — if the method is unknown (a retired method
       or a typo), fall back to the global default instead of raising
       :class:`KeyError`.

    The class is intentionally a *value object* (frozen-ish: no public
    setters, all attributes populated in the constructor). That keeps
    the resolve logic easy to unit-test and rules out any hidden
    side-effect on the registry.
    """

    def __init__(
        self,
        per_method_timeout_seconds: Optional[Dict[str, int]] = None,
        global_default_timeout_seconds: int = DEFAULT_GLOBAL_TIMEOUT_SECONDS,
        parallelism_cap: int = DEFAULT_PARALLELISM_CAP,
    ) -> None:
        self._per_method: Dict[str, int] = dict(per_method_timeout_seconds or {})
        self._global_default: int = int(global_default_timeout_seconds)
        self.parallelism_cap: int = int(parallelism_cap)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def defaults(cls) -> "TimeoutPolicy":
        """Build a policy populated only with the hard-coded defaults.

        Useful in tests and as a last-resort fallback when yaml loading
        fails entirely.
        """
        return cls(
            per_method_timeout_seconds=dict(DEFAULT_PER_METHOD_TIMEOUT_SECONDS),
            global_default_timeout_seconds=DEFAULT_GLOBAL_TIMEOUT_SECONDS,
            parallelism_cap=DEFAULT_PARALLELISM_CAP,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimeoutPolicy":
        """Build a policy from an already-parsed mapping.

        Tolerates missing keys — falls back to the hard-coded defaults
        for any absent field. This is what the test suite and the
        registry both call once the yaml is in hand.
        """
        execution = data.get("execution", {}) if isinstance(data, Mapping) else {}
        if not isinstance(execution, Mapping):
            execution = {}

        per_method_raw = execution.get("per_method_timeout_seconds", {})
        if not isinstance(per_method_raw, Mapping):
            per_method_raw = {}

        per_method: Dict[str, int] = {}
        for method, value in per_method_raw.items():
            try:
                per_method[str(method)] = int(value)
            except (TypeError, ValueError):
                # Skip silently — a single bad entry should not brick the
                # whole policy. The method will then fall through to the
                # global default at resolve time.
                continue

        parallelism = execution.get("parallelism_cap", DEFAULT_PARALLELISM_CAP)
        try:
            parallelism_int = int(parallelism)
        except (TypeError, ValueError):
            parallelism_int = DEFAULT_PARALLELISM_CAP

        return cls(
            per_method_timeout_seconds=per_method,
            global_default_timeout_seconds=DEFAULT_GLOBAL_TIMEOUT_SECONDS,
            parallelism_cap=parallelism_int,
        )

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "TimeoutPolicy":
        """Build a policy from a yaml file on disk.

        Behaviour matrix:

        - File missing → return :meth:`defaults` (hard-coded fallbacks).
        - PyYAML missing → return :meth:`defaults` (hard-coded fallbacks).
        - File present but malformed → return :meth:`defaults`
          (we never raise on a missing/malformed config — the agent
          should keep running with the documented hard-coded numbers).
        - File present and well-formed → :meth:`from_dict` builds the
          policy from ``execution.per_method_timeout_seconds`` and
          ``execution.parallelism_cap``.

        The signature accepts either a string path or a :class:`Path`
        so callers don't have to coerce.
        """
        path = Path(yaml_path)
        if not path.exists():
            return cls.defaults()

        try:
            import yaml  # local import: keeps the module importable even
                         # if PyYAML is missing at startup
        except ImportError:
            return cls.defaults()

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return cls.defaults()

        if not isinstance(raw, Mapping):
            return cls.defaults()

        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(
        self,
        verification_method: str,
    ) -> int:
        """Resolve the timeout (in seconds) for a verification method.

        2026-09-13: the ``vp_payload["timeout_seconds"]`` override
        parameter was DELETED — a legacy plan value (VP-023's 120s)
        must never shrink the enforcement caps again. The return value
        is surface metadata for ``vp_start`` events and always equals
        the flat 1-hour cap in practice.

        Resolution order:

        1. ``per_method_timeout_seconds[verification_method]`` if present.
        2. ``global_default_timeout_seconds`` as a last-resort fallback.

        The second branch is what makes the unknown-method case a soft
        fallback rather than a :class:`KeyError`. It also covers the
        edge case of a yaml that omitted the relevant method.

        Args:
            verification_method: e.g. ``"ui_validation"``. Unknown
                values (including the empty string) just fall through
                to the global default.

        Returns:
            A positive integer number of seconds.
        """
        method_key = (verification_method or "").strip()
        if method_key and method_key in self._per_method:
            return self._per_method[method_key]

        return self._global_default

    # ------------------------------------------------------------------
    # Introspection (handy for tests and debugging)
    # ------------------------------------------------------------------

    def per_method(self) -> Dict[str, int]:
        """Return a copy of the per-method map (read-only snapshot)."""
        return dict(self._per_method)

    def global_default(self) -> int:
        """Return the global default timeout in seconds."""
        return self._global_default
