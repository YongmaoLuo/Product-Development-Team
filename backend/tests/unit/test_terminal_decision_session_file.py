"""The sidecar file that carries a terminal decision's session and analysis.

When a verification round stops and asks a human what to do, the answer
travels with a **session id** — the id of the analysis conversation that
produced the recommendation — and with the **analysis context** the
recommendation was read against.  The execution side needs both of them
again later (to resume the analysis session, to re-render the context
next to the question), and the decision itself lives in the state
database.

This module owns a third thing: a plain JSON file next to the plan,
``<plan_dir>/terminal_decision_session.json``.  It is a **sidecar** —
it does not change the decision storage schema (arch decision point 8),
it just parks the two things the decision table cannot hold.

What these tests pin down
=========================

* **The round trip is lossless.**  What was written is what is read
  back — the session id, the analysis context, the failed points.
* **A missing file is not an error.**  There are rounds that never
  reached a terminal decision, and a plan directory that has no
  sidecar is a normal state, not a failure (arch decision point 5:
  read it or degrade, never raise).
* **A corrupt file is not an error either.**  Same reason: the reader
  degrades to "there is no session record", it does not crash the
  execution that is trying to resume one.
* **An unknown schema version is not an error.**  A file written by a
  newer version of this module is *not* silently read as if it were the
  current schema — it is refused, which is the same "no record" answer.
* **The write is atomic.**  The bytes go to a sibling ``.tmp`` file and
  are then renamed onto the final path, so a reader never sees half a
  record; and no ``.tmp`` file is left behind afterwards.

The module is deliberately **stdlib-only** and imports no orchestrator,
no routes and no server: it is a file-level helper that the orchestrator
will use, and an import the other way round would be a cycle.
"""

import ast
import json
import os
from pathlib import Path

import pytest

from verification import terminal_decision_session as tds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _analysis():
    """A full analysis context — the shape the judge reads."""
    return {
        "context": "第三轮验证停在登录接口",
        "problem": "登录接口返回 500，不是预期结果",
        "evidence": ["VP-3 失败"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "缺陷已定性，重跑不会改变结果",
        "uncertainty": "没有看过服务端日志",
    }


def _failed_points():
    """One failed verification point, with the facts the analysis quotes."""
    return [
        {
            "id": "VP-3",
            "title": "登录接口",
            "expected_result": "返回 200",
            "failure_class": "deliverable_defect",
            "consecutive_rounds": 3,
            "verdict": "expected 200, got 500",
        }
    ]


def _record(**overrides):
    """A record built by the module under test."""
    kwargs = {
        "decision_id": "d1",
        "plan_id": "p1",
        "session_id": "sess-abc",
        "analysis": _analysis(),
        "failed_points": _failed_points(),
        "round_number": 3,
    }
    kwargs.update(overrides)
    return tds.build_record(**kwargs)


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_session_and_analysis(tmp_path):
    """What was written is what is read back — nothing dropped, nothing added."""
    record = _record()

    tds.save(tmp_path, record)

    assert tds.load(tmp_path) == record

    path = tmp_path / tds.SESSION_FILENAME
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == tds.SCHEMA_VERSION
    assert payload["decision_id"] == "d1"
    assert payload["plan_id"] == "p1"
    assert payload["session_id"] == "sess-abc"
    assert payload["analysis"] == _analysis()
    assert payload["failed_points"] == _failed_points()
    assert payload["stop_reason"] == "repair_execution_failed"
    assert payload["round_number"] == 3


def test_round_trip_keeps_analysis_verbatim(tmp_path):
    """The analysis context is stored as given — not re-shaped, not summarised."""
    analysis = _analysis()
    analysis["evidence"] = ["VP-3 失败", "VP-7 超时"]

    record = _record(analysis=analysis)
    tds.save(tmp_path, record)

    loaded = tds.load(tmp_path)
    assert loaded["analysis"] == analysis
    assert loaded["analysis"]["evidence"] == ["VP-3 失败", "VP-7 超时"]


# ---------------------------------------------------------------------------
# Degrading instead of raising
# ---------------------------------------------------------------------------


def test_missing_file_returns_none(tmp_path):
    """A plan directory with no sidecar is a normal state, not a failure."""
    assert tds.load(tmp_path) is None


def test_corrupt_json_returns_none(tmp_path):
    """A half-written or garbage file degrades to "no record"."""
    (tmp_path / tds.SESSION_FILENAME).write_text("{not json", encoding="utf-8")

    assert tds.load(tmp_path) is None


def test_unknown_schema_version_returns_none(tmp_path):
    """A file written by a newer schema is refused, not misread."""
    path = tmp_path / tds.SESSION_FILENAME
    path.write_text(
        json.dumps({"schema_version": 99, "decision_id": "d1"}), encoding="utf-8"
    )

    assert tds.load(tmp_path) is None


def test_top_level_not_a_mapping_returns_none(tmp_path):
    """Valid JSON that is not an object is not a record."""
    path = tmp_path / tds.SESSION_FILENAME
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert tds.load(tmp_path) is None


# ---------------------------------------------------------------------------
# The atomic write
# ---------------------------------------------------------------------------


def test_save_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    """The bytes are staged in a sibling tmp file and renamed into place."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen["src"] = Path(src)
        seen["dst"] = Path(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(tds.os, "replace", spy)

    tds.save(tmp_path, _record())

    target = tmp_path / tds.SESSION_FILENAME
    assert seen["dst"] == target
    assert seen["src"] == tmp_path / f".{tds.SESSION_FILENAME}.tmp"

    # The tmp file is gone, and nothing else is left in the directory.
    assert not seen["src"].exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [tds.SESSION_FILENAME]


def test_save_leaves_no_tmp_when_the_write_fails(tmp_path, monkeypatch):
    """A failed write does not leave a stray tmp file behind."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tds.os, "replace", boom)

    with pytest.raises(OSError):
        tds.save(tmp_path, _record())

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The record's shape
# ---------------------------------------------------------------------------


def test_build_record_has_fixed_keys_and_defaults():
    """The nine keys are always there, and their defaults never move."""
    record = tds.build_record(decision_id="d2", plan_id="p2")

    assert set(record) == {
        "schema_version",
        "decision_id",
        "plan_id",
        "session_id",
        "analysis",
        "failed_points",
        "stop_reason",
        "round_number",
        "created_at",
    }
    assert record["schema_version"] == tds.SCHEMA_VERSION
    assert record["decision_id"] == "d2"
    assert record["plan_id"] == "p2"
    assert record["session_id"] is None
    assert record["analysis"] == {}
    assert record["failed_points"] == []
    assert record["stop_reason"] == "repair_execution_failed"
    assert record["round_number"] == 0
    assert record["created_at"]


def test_session_id_none_is_written_as_json_null(tmp_path):
    """A missing session id is written as null — the context stays complete."""
    record = _record(session_id=None, analysis=_analysis())
    tds.save(tmp_path, record)

    raw = (tmp_path / tds.SESSION_FILENAME).read_text(encoding="utf-8")
    assert '"session_id": null' in raw

    loaded = tds.load(tmp_path)
    assert loaded["session_id"] is None
    # The context is complete even without a session to resume.
    assert loaded["analysis"] == _analysis()


def test_explicit_created_at_is_kept(tmp_path):
    """A caller that stamps its own time keeps it."""
    record = _record(created_at="2026-11-05T00:00:00")
    assert record["created_at"] == "2026-11-05T00:00:00"

    tds.save(tmp_path, record)
    assert tds.load(tmp_path)["created_at"] == "2026-11-05T00:00:00"


def test_session_path_points_at_the_sidecar(tmp_path):
    """The path is derived from the plan directory, not guessed."""
    assert tds.session_path(tmp_path) == tmp_path / "terminal_decision_session.json"


# ---------------------------------------------------------------------------
# The module's own boundaries
# ---------------------------------------------------------------------------


def test_module_imports_stdlib_only():
    """No orchestrator, no routes, no server — stdlib only.

    The orchestrator will import *this* module; an import the other way
    round would be a cycle, and pulling the server in would make a file
    helper untestable in isolation.
    """
    source = Path(tds.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import is by definition not stdlib.
                roots.add("." + (node.module or ""))
            else:
                roots.add((node.module or "").split(".")[0])

    allowed = {
        "__future__",
        "collections",
        "datetime",
        "json",
        "os",
        "pathlib",
        "typing",
    }
    assert roots <= allowed, f"non-stdlib imports: {sorted(roots - allowed)}"