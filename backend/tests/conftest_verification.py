"""
Shared fixtures for the verification-agent test suite.

This conftest provides three reusable pieces of scaffolding:

* :func:`make_vp` — factory for a single ``verification_point`` dict.
  Defaults are pinned to the values the rest of the test suite relies
  on (single-clause ``expected_result`` so the splitter won't decompose
  it, ``fake_sleep_seconds=1.0`` to match :class:`FakeVerifierBackend`'s
  default, ``should_fail=False``).

* :func:`make_verification_plan` — wraps a list of VPs in the standard
  ``{"verification_points": [...], "plan_id": "..."}`` envelope that
  :class:`VerificationAgent` expects.

* :func:`parse_execution_log` — streaming reader over the JSON-lines
  log files the persistence layer writes (``logs/verification_*``,
  ``execution.log``). The returned object supports ``.filter(event=...)``
  and ``.group_by(event, key)`` queries so tests can assert on subsets
  of the log without loading the whole file into a list.

These functions live in a regular module rather than as pytest
fixtures so they can be called with explicit arguments (most tests
need several VPs in one plan, which is awkward as a fixture).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union


# ---------------------------------------------------------------------------
# VP factory
# ---------------------------------------------------------------------------


def make_vp(
    id: str = "VP-001",
    title: str = "测试验证点",
    verification_method: str = "automated_test",
    expected_result: str = "测试通过",
    priority: str = "medium",
    fake_sleep_seconds: float = 1.0,
    should_fail: bool = False,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a single ``verification_point`` dict with sensible defaults.

    The default ``expected_result="测试通过"`` is a single clause (no
    ``;`` separator) so :class:`SplitDecision` will not decompose it
    on timeout — most tests want the single-VP path, not the
    split-children path.

    Extra keyword arguments are merged into the returned dict, so
    tests can override ``test_command``, ``target_url``, etc.
    without this factory growing a long parameter list.
    """
    vp: Dict[str, Any] = {
        "id": id,
        "title": title,
        "verification_method": verification_method,
        "priority": priority,
        "expected_result": expected_result,
        "fake_sleep_seconds": fake_sleep_seconds,
        "should_fail": should_fail,
    }
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# Plan factory
# ---------------------------------------------------------------------------


def make_verification_plan(
    verification_points: Optional[Iterable[Dict[str, Any]]] = None,
    plan_id: str = "test-plan",
    **extra: Any,
) -> Dict[str, Any]:
    """Wrap a list of VPs in the standard plan envelope.

    The envelope is ``{"verification_points": [...], "plan_id": "..."}``,
    matching the shape :class:`VerificationAgent` reads and
    :func:`ExecutionProfileGenerator` consumes. The ``plan_id`` is
    kept on the envelope (rather than buried in the VPs) so log
    parsers can attribute events to a plan without scanning the
    file for the parent dir name.
    """
    vps: List[Dict[str, Any]] = list(verification_points or [])
    plan: Dict[str, Any] = {
        "plan_id": plan_id,
        "verification_points": vps,
    }
    plan.update(extra)
    return plan


# ---------------------------------------------------------------------------
# Streaming log parser
# ---------------------------------------------------------------------------


class _ExecutionLogStream:
    """Lazy streaming view over a JSON-lines execution log.

    The class is intentionally minimal: it does not parse the log on
    construction, only when a query method is called. This keeps the
    cost of opening a 100MB log at "zero" — only the rows a test
    actually inspects get parsed.

    The query methods are deliberately not chainable: a test that
    wants ``filter().group_by()`` can just call them in sequence
    (the second call re-reads the file). Chaining would require
    buffering intermediate results, which defeats the point of a
    streaming API.
    """

    def __init__(self, log_path: Union[str, Path]):
        self.log_path = Path(log_path)

    def _iter_rows(self) -> Iterator[Dict[str, Any]]:
        """Yield parsed JSON rows from the log file, line by line.

        Skips blank lines and tolerates ``json.JSONDecodeError`` on
        a single row by simply not yielding it — log files in this
        codebase are always written by the persistence layer, but
        partial writes / truncations can leave a trailing invalid
        row, and a streaming parser should not fail the whole
        iteration over a single bad line.
        """
        if not self.log_path.exists():
            return
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row

    def filter(self, event: Optional[str] = None, **match: Any) -> List[Dict[str, Any]]:
        """Return rows matching the given predicates.

        Parameters
        ----------
        event:
            If provided, only rows whose ``event`` field equals this
            value are returned.
        **match:
            Additional equality predicates on other fields. A row
            must match *all* predicates (logical AND) to be
            included.

        Returns:
            A list of matching rows, in file order.
        """
        results: List[Dict[str, Any]] = []
        for row in self._iter_rows():
            if event is not None and row.get("event") != event:
                continue
            if any(row.get(k) != v for k, v in match.items()):
                continue
            results.append(row)
        return results

    def group_by(
        self, event: Optional[str] = None, key: str = "vp_id"
    ) -> Dict[Any, Dict[str, Any]]:
        """Group rows by the value of ``key`` and return one row per group.

        When multiple rows share the same key, the **last** row wins
        (later events typically carry the most up-to-date state —
        e.g. ``vp_completed`` arrives after ``vp_started``). The
        "first wins" alternative would force callers to remember
        the iteration order, which is easy to get wrong.

        Parameters
        ----------
        event:
            If provided, only rows whose ``event`` field equals this
            value are considered.
        key:
            The field to group by. Defaults to ``"vp_id"`` because
            that's the most common grouping for verification logs.

        Returns:
            A ``dict`` keyed by the value of ``row[key]`` for each
            row in the filtered set.
        """
        grouped: Dict[Any, Dict[str, Any]] = {}
        for row in self.filter(event=event):
            k = row.get(key)
            if k is None:
                continue
            grouped[k] = row
        return grouped


def parse_execution_log(
    log_path: Union[str, Path]
) -> _ExecutionLogStream:
    """Open a JSON-lines execution log as a streaming query object.

    The returned :class:`_ExecutionLogStream` exposes
    :meth:`~_ExecutionLogStream.filter` and
    :meth:`~_ExecutionLogStream.group_by` so tests can write
    stream-style queries::

        stream = parse_execution_log("plans/x/logs/verification_1.log")
        ui_starts = stream.filter(event="group_started", method="ui_validation")
        last_per_vp = stream.group_by(event="vp_completed", key="vp_id")
    """
    return _ExecutionLogStream(log_path)
