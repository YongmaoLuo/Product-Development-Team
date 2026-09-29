"""2026-09-14 auto-loop: 纯拆分轮跳过执行阶段。

用户指令::

    "如果他没有拆出新的修复任务，那执行状态就没有什么东西可以执行，
     他就直接跳过，他就会重新进入到验证状态，然后就会去执行他那些
     拆分后的 VP。"

A round that judged every failed VP "too big, split it" produces
``vp_splits`` and no repair tasks. Before this change that combination
hit the ``no_repair_tasks`` dead-end branch and terminalised the plan —
with the freshly written split children never verified. Now:

  * the dead-end branch is skipped when the round is a pure split
    (``_round_is_pure_split``);
  * the execution phase is skipped entirely (no executor subprocess —
    the children are *verified*, not implemented) and the chain
    re-enters verification through the existing post-repair callback;
  * the post-repair path has the same branch, guarded by the round cap so
    a split→verify→split chain cannot recurse forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import server as server_mod
from tests.app_source import app_source


# ---------------------------------------------------------------------------
# pure helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "result,expected",
    [
        ({"vp_splits": [{"vp_id": "VP-023"}], "repair_tasks": []}, True),
        ({"vp_splits": [{"vp_id": "VP-023"}]}, True),          # no key at all
        ({"vp_splits": [{"vp_id": "VP-023"}],
          "repair_tasks": [{"id": "repair-r2-01"}]}, False),   # mixed round
        ({"vp_splits": [], "repair_tasks": []}, False),
        ({"repair_tasks": []}, False),
        ({}, False),
        ({"vp_splits": None, "repair_tasks": []}, False),
        ({"vp_splits": "nope", "repair_tasks": []}, False),
    ],
)
def test_round_is_pure_split(result, expected):
    assert server_mod._round_is_pure_split(result) is expected


# ---------------------------------------------------------------------------
# structural pins (the auto-loop is a 900-line closure; the repo's
# convention for pinning behaviour inside it is a source check — see
# tests/unit/test_repair_callback_imports.py)
# ---------------------------------------------------------------------------

def _server_source() -> str:
    """The whole application, not just ``server.py`` — the auto-loop lives in
    ``backend/verification_loop.py`` since the 2026-09-25 split."""
    return app_source()


def test_dead_end_branch_skipped_for_pure_split():
    """An empty repair list caused by a split must not terminate the
    plan — the guard must be on the dead-end condition."""
    src = _server_source()
    assert "_pure_split = _round_is_pure_split(result)" in src
    assert "if not repair_tasks and not _pure_split:" in src


def test_pure_split_dispatch_skips_executor():
    """The pure-split path must call the callback directly and return —
    no executor subprocess spawn on that path."""
    src = _server_source()
    start = src.find("            if _pure_split:")
    assert start >= 0, "pure-split dispatch block not found"
    # The block ends at its own ``return``; the ordinary
    # ``_run_repair_execution_async`` call sits AFTER it.
    end = src.find("            return", start)
    assert end > start, "pure-split dispatch must end with a return"
    block = src[start:end]
    assert "_on_repair_complete(0)" in block, (
        "pure split must re-enter verification via the shared callback"
    )
    assert "_run_repair_execution_async(" not in block, (
        "pure split must NOT spawn the executor — there is nothing to run"
    )


def test_post_repair_pure_split_is_capped():
    """Inside the callback, a pure split chains another round but stops
    at the round cap (no unbounded split→verify recursion)."""
    src = _server_source()
    cb_start = src.find("def _on_repair_complete")
    assert cb_start >= 0
    # Bound the window at the spawn that follows the callback's definition.
    cb_end = src.find("# 2026-09-11 plan v14: replace synchronous", cb_start)
    assert cb_end > cb_start, "callback end marker not found"
    body = src[cb_start:cb_end]
    assert "elif _round_is_pure_split(_result):" in body, (
        "post-repair round must handle the pure-split case"
    )
    assert "if _next_round >= _new_max:" in body, (
        "the split→verify chain must be bounded by the round cap"
    )
