"""
TDD tests for the binary-freshness post-check.

Background
----------
An earlier plan: a run of commits modified
``native_ext/src/core.rs`` (the Rust→Python PyO3 binding source) but
the project's ``venv1/lib/python3.11/site-packages/native_ext/*.so``
predated all of them — so every verification point that exercised the
binding was running the old build.
Every verification VP that ran against the live API exercised the
stale binary; the downstream user-visible signal never appeared;
the plan finished with ``verification_loop_stopped / max_rounds_reached``
without any hint that the binding itself was the culprit.

The fix appends a ``VP-binary-freshness`` entry to the verification
report after Phase 3. The four TDD tests below pin the contract:

  1. ``test_appends_freshness_pass_for_rust_python_fresh_binary``
     A Rust/PyO3 project with a fresh binary gets a PASS entry
     appended to ``verification_results`` and the overall status
     is left untouched.

  2. ``test_appends_freshness_fail_for_rust_python_stale_binary``
     A Rust/PyO3 project whose ``.so`` is older than its ``.rs``
     files gets a FAILED entry AND ``overall_status`` is downgraded
     to ``FAILED`` even if every other VP passed.

  3. ``test_appends_freshness_no_op_for_pure_python_project``
     A pure-Python project gets a bytecode-cache freshness entry
     appended; stale bytecode downgrades ``overall_status``.

  4. ``test_appends_freshness_no_op_when_project_has_no_artefacts``
     A project with neither Cargo.toml nor ``.py`` files (e.g. a
     JS-only repo) gets a no-op PASS entry so downstream consumers
     see a uniform shape.

Design note: the check runs AFTER ``generate_verification_report``
rather than as the first VP of the LLM-generated plan, because
prepending would shift every LLM-generated VP id by +1 and break
the many test fixtures that mock LLM responses keyed on the
LLM-original VP ids. Appending after Phase 3 keeps the LLM contract
untouched and still catches the same failure mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch", "main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )


def _make_agent(project_dir: Path):
    from verification_agent import VerificationAgent

    return VerificationAgent(
        plan_dir=project_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _setup_rust_python(project_dir: Path, *, fresh: bool):
    """Create a Cargo.toml + native_ext/src/.rs + native_ext/target/release/lib.so.

    ``fresh=True`` makes the .so mtime NEWER than the .rs.
    ``fresh=False`` makes the .so mtime OLDER than the .rs (the
    stale-binary failure mode).
    """
    _git_init(project_dir)
    pkg = project_dir / "native_ext"
    (pkg / "src").mkdir(parents=True, exist_ok=True)
    (pkg / "target" / "release").mkdir(parents=True, exist_ok=True)
    (pkg / "Cargo.toml").write_text(
        "[package]\nname = \"native_ext\"\nversion = \"0.1.0\"\n",
        encoding="utf-8",
    )
    rs_path = pkg / "src" / "lib.rs"
    rs_path.write_text("// stub\n", encoding="utf-8")
    so_path = pkg / "target" / "release" / "lib.so"
    so_path.write_text("// stub .so\n", encoding="utf-8")
    if fresh:
        os.utime(rs_path, (time.time() - 3600, time.time() - 3600))
        os.utime(so_path, (time.time(), time.time()))
    else:
        os.utime(rs_path, (time.time(), time.time()))
        os.utime(so_path, (time.time() - 3600, time.time() - 3600))


def _setup_pure_python(project_dir: Path, *, fresh: bool):
    _git_init(project_dir)
    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    py_path = project_dir / "src" / "main.py"
    py_path.write_text("# stub\n", encoding="utf-8")
    cache = project_dir / "src" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    pyc = cache / "main.cpython-311.pyc"
    pyc.write_text("# cached\n", encoding="utf-8")
    if fresh:
        os.utime(py_path, (time.time() - 3600, time.time() - 3600))
        os.utime(pyc, (time.time(), time.time()))
    else:
        os.utime(py_path, (time.time(), time.time()))
        os.utime(pyc, (time.time() - 3600, time.time() - 3600))


def _setup_js_only(project_dir: Path):
    _git_init(project_dir)
    (project_dir / "package.json").write_text("{}\n", encoding="utf-8")


def _baseline_report(status: str = "PASSED") -> Dict[str, Any]:
    return {
        "overall_status": status,
        "verification_results": [
            {"id": "VP-001", "status": "PASSED", "title": "Smoke"},
        ],
        "requirement_deviations": [],
    }


def test_appends_freshness_pass_for_rust_python_fresh_binary(tmp_path):
    """A Rust/PyO3 project with a fresh binary gets a PASS entry
    appended to ``framework_checks`` and the overall status is
    left untouched. The freshness entry lives in ``framework_checks``
    (not ``verification_results``) so downstream consumers that
    count VPs by hand are not confused by framework-injected
    guard rails."""
    _setup_rust_python(tmp_path, fresh=True)
    agent = _make_agent(tmp_path)
    report = _baseline_report(status="PASSED")

    out = agent._append_binary_freshness_result(report, plan_data={})

    freshness = next(
        (r for r in out.get("framework_checks", []) if r["id"] == "VP-binary-freshness"),
        None,
    )
    assert freshness is not None, (
        f"freshness entry must be appended to framework_checks; got: {out}"
    )
    assert freshness["status"] == "PASSED", (
        f"fresh binary must produce PASS; got {freshness}"
    )
    # Critical: the freshness entry must NOT bleed into the
    # LLM-authored ``verification_results`` list, otherwise
    # downstream VP-count assertions break.
    assert all(
        r.get("id") != "VP-binary-freshness"
        for r in out["verification_results"]
    ), "freshness entry must live in framework_checks, not verification_results"
    assert out["overall_status"] == "PASSED", (
        "freshness PASS must NOT downgrade overall_status when it was PASSED"
    )


def test_appends_freshness_fail_for_rust_python_stale_binary(tmp_path):
    """A Rust/PyO3 project whose ``.so`` is older than its ``.rs``
    files gets a FAILED entry AND ``overall_status`` is downgraded
    to ``FAILED`` even if every other VP passed."""
    _setup_rust_python(tmp_path, fresh=False)
    agent = _make_agent(tmp_path)
    report = _baseline_report(status="PASSED")

    out = agent._append_binary_freshness_result(report, plan_data={})

    freshness = next(
        (r for r in out.get("framework_checks", []) if r["id"] == "VP-binary-freshness"),
        None,
    )
    assert freshness is not None
    assert freshness["status"] == "FAILED", (
        f"stale binary must produce FAILED; got {freshness}"
    )
    assert "stale" in freshness["title"].lower() or "stale" in freshness["actual_result"].lower(), (
        f"failure entry must clearly identify the binary as stale; got {freshness}"
    )
    assert "maturin" in freshness.get("evidence", "") or "cargo build" in freshness.get("evidence", ""), (
        "evidence must include the rebuild command so the operator who "
        "reads the failure knows what to do."
    )
    assert out["overall_status"] == "FAILED", (
        f"stale binary must downgrade overall_status to FAILED; got "
        f"{out['overall_status']!r}. This is the stale-binary "
        f"failure mode the post-check exists to catch."
    )


def test_appends_freshness_fail_for_pure_python_stale_bytecode(tmp_path):
    """A pure-Python project with stale ``.pyc`` downgrades the
    overall status."""
    _setup_pure_python(tmp_path, fresh=False)
    agent = _make_agent(tmp_path)
    report = _baseline_report(status="PASSED")

    out = agent._append_binary_freshness_result(report, plan_data={})

    freshness = next(
        (r for r in out.get("framework_checks", []) if r["id"] == "VP-binary-freshness"),
        None,
    )
    assert freshness is not None
    assert freshness["status"] == "FAILED", (
        f"stale bytecode must produce FAILED; got {freshness}"
    )
    assert out["overall_status"] == "FAILED", (
        "stale bytecode must downgrade overall_status to FAILED"
    )


def test_appends_freshness_pass_for_pure_python_fresh_bytecode(tmp_path):
    """A pure-Python project with fresh bytecode gets a PASS
    entry; the overall status is left untouched."""
    _setup_pure_python(tmp_path, fresh=True)
    agent = _make_agent(tmp_path)
    report = _baseline_report(status="PASSED")

    out = agent._append_binary_freshness_result(report, plan_data={})

    freshness = next(
        (r for r in out.get("framework_checks", []) if r["id"] == "VP-binary-freshness"),
        None,
    )
    assert freshness is not None
    assert freshness["status"] == "PASSED"
    assert out["overall_status"] == "PASSED"


def test_appends_freshness_no_op_when_project_has_no_artefacts(tmp_path):
    """A JS-only project gets a no-op PASS entry."""
    _setup_js_only(tmp_path)
    agent = _make_agent(tmp_path)
    report = _baseline_report(status="PASSED")

    out = agent._append_binary_freshness_result(report, plan_data={})

    freshness = next(
        (r for r in out.get("framework_checks", []) if r["id"] == "VP-binary-freshness"),
        None,
    )
    assert freshness is not None
    assert freshness["status"] == "PASSED"
    assert "no-op" in freshness["title"].lower() or "no-op" in freshness.get("actual_result", "").lower(), (
        f"no-artefact entry must mention no-op; got {freshness}"
    )


def test_does_not_pollute_verification_results():
    """Regression guard: the freshness entry must NEVER land in
    ``verification_results``. That field is the LLM-authored Phase-3
    envelope; mixing framework-injected guard rails into it would
    inflate the rendered VP count and break consumers that parse
    ``verification_results`` directly (the bridge UI, the task_sync
    card, and any downstream test that counts VPs by hand).
    """
    # Pure-Python fresh project: the freshness entry is a no-op PASS,
    # so any pollution would show up as a second PASS in
    # ``verification_results``.
    import tempfile as _tempfile
    tmp = _tempfile.mkdtemp()
    try:
        from pathlib import Path as _P
        p = _P(tmp)
        _setup_pure_python(p, fresh=True)
        from verification_agent import VerificationAgent
        agent = VerificationAgent(plan_dir=p, project_dir=p, coding_tool=None)
        baseline = _baseline_report(status="PASSED")
        out = agent._append_binary_freshness_result(baseline, plan_data={})
        assert len(out["verification_results"]) == len(baseline["verification_results"]), (
            f"freshness must NOT add to verification_results; "
            f"baseline={len(baseline['verification_results'])}, "
            f"after={len(out['verification_results'])}"
        )
        assert all(
            r.get("id") != "VP-binary-freshness"
            for r in out["verification_results"]
        )
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)