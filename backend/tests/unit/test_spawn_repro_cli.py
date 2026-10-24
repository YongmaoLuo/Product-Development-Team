"""Pins for the spawn reproduction command line.

What is under test
------------------
``backend/tests/manual/spawn_repro.py`` -- the command line that drives
the spawn reproduction core: the argument defaults, the two assertions
(``--assert-failure-rate`` and ``--assert-single-cause``), the matrix
that measures every candidate with its parameter on and off, and the
tiny service the ``tiny`` target spawns.

Why the modules are loaded by path
----------------------------------
``backend/tests/manual/`` is a namespace-package directory (PEP 420 --
no ``__init__.py``), so it is not an ordinary package to import.  The
modules are loaded from their files, which makes the test independent
of which ``tests`` package the interpreter happens to resolve first.

The pins
--------
1. The defaults are the ones the manual runs use: the raw profile, the
   tiny target, ten spawns, an eight-second readiness budget.
2. ``--assert-failure-rate`` is a floor: a rate at or above it holds.
   A threshold of zero is not "no threshold" -- it means *no* failure
   may happen at all.
3. The matrix measures every candidate twice -- the parameter on and
   the parameter off -- so three candidates are six cells.
4. Exactly one candidate moving the failure rate is a cause; two are
   an ambiguous sample, and the tool says so instead of picking one.
5. Importing the tiny service starts nothing.  A module that listens
   on import cannot be imported by a test that is checking whether
   anything listens.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Set

import pytest

#: The backend package root -- ``<repo>/backend``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: The manual tools under test.
_MANUAL_DIR = Path(__file__).resolve().parents[1] / "manual"
_HARNESS_PATH = _MANUAL_DIR / "spawn_repro.py"
_TINY_PATH = _MANUAL_DIR / "tiny_service.py"


def _load(path: Path, name: str) -> ModuleType:
    """Import a module from its file."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_repro = _load(_HARNESS_PATH, "spawn_repro_cli_under_test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batch(runs: int, failed: int):
    """A batch outcome, as :func:`run_batch` would have returned it."""
    return spawn_repro.BatchResult(
        runs=runs, ready=runs - failed, failed=failed, results=[],
    )


def _stub_batch(failed: int) -> Callable[..., object]:
    """A ``run_batch`` that always reports *failed* failures."""

    def _fake(cfg, runs):
        return _batch(runs, failed)

    return _fake


def _cell_of(cfg) -> str:
    """Which matrix cell a config is: ``"<candidate>-on"`` or ``"-off"``.

    The production shape is the same for every candidate's "off" cell,
    so the candidate can only be told apart on the "on" cells -- which
    is exactly what a stub needs to answer differently per candidate.
    """
    if not cfg.start_new_session:
        return "start_new_session-on"
    if cfg.stdin_mode == "inherit":
        return "stdin-on"
    if cfg.parent_threads:
        return "parent_threads-on"
    return "off"


def _stub_matrix(failing: Set[str]) -> Callable[..., object]:
    """A ``run_batch`` where the named candidates fail 3 out of 5.

    "Off" cells never fail, so a candidate that fails only when its
    parameter is on is the one the failure follows.
    """

    def _fake(cfg, runs):
        cell = _cell_of(cfg)
        candidate = cell.split("-")[0]
        failed = 3 if (cell.endswith("-on") and candidate in failing) else 0
        return _batch(runs, failed)

    return _fake


# ---------------------------------------------------------------------------
# 1. The defaults
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    """No arguments means the shape the manual runs use."""
    args = spawn_repro.parse_args([])

    assert args.profile == "raw"
    assert args.target == "tiny"
    assert args.runs == 10
    assert args.ready_timeout == 8


# ---------------------------------------------------------------------------
# 2. The failure-rate assertion
# ---------------------------------------------------------------------------


def test_assert_failure_rate_gt_zero(monkeypatch, tmp_path):
    """A rate at or above the threshold holds; one below it does not."""
    out = tmp_path / "rate.json"

    monkeypatch.setattr(spawn_repro, "run_batch", _stub_batch(6))
    assert spawn_repro.main([
        "--assert-failure-rate", "0.5", "--json-out", str(out),
    ]) == 0

    monkeypatch.setattr(spawn_repro, "run_batch", _stub_batch(4))
    assert spawn_repro.main([
        "--assert-failure-rate", "0.5", "--json-out", str(out),
    ]) == 1


def test_assert_failure_rate_zero(monkeypatch, tmp_path):
    """A threshold of zero means no failure may happen at all."""
    out = tmp_path / "zero.json"

    monkeypatch.setattr(spawn_repro, "run_batch", _stub_batch(1))
    assert spawn_repro.main([
        "--assert-failure-rate", "0", "--json-out", str(out),
    ]) == 1

    monkeypatch.setattr(spawn_repro, "run_batch", _stub_batch(0))
    assert spawn_repro.main([
        "--assert-failure-rate", "0", "--json-out", str(out),
    ]) == 0


# ---------------------------------------------------------------------------
# 3. The matrix
# ---------------------------------------------------------------------------


def test_matrix_builds_six_cells(monkeypatch, tmp_path):
    """Three candidates, each measured on and off, are six cells."""
    calls = []

    def _fake(cfg, runs):
        calls.append((cfg, runs))
        return _batch(runs, 0)

    monkeypatch.setattr(spawn_repro, "run_batch", _fake)
    out = tmp_path / "matrix.json"

    assert spawn_repro.main([
        "--matrix", "--runs-per-cell", "3", "--json-out", str(out),
    ]) == 0

    assert len(calls) == 6, "three candidates are six cells, not three"
    assert all(runs == 3 for _, runs in calls)

    payload = json.loads(out.read_text(encoding="utf-8"))
    matrix = payload["matrix"]
    assert set(matrix) == set(spawn_repro.MATRIX_CANDIDATES)
    for candidate, cell in matrix.items():
        assert set(cell) == {"on", "off", "verdict"}
        assert cell["on"]["runs"] == 3
        assert cell["off"]["runs"] == 3
        assert cell["verdict"] in {"因果", "无关"}


def test_matrix_flags_single_cause(monkeypatch, tmp_path):
    """One candidate that moves the rate is a cause; two are ambiguous."""
    out = tmp_path / "cause.json"

    monkeypatch.setattr(
        spawn_repro, "run_batch", _stub_matrix({"start_new_session"}),
    )
    assert spawn_repro.main([
        "--matrix", "--assert-single-cause", "--json-out", str(out),
    ]) == 0

    monkeypatch.setattr(
        spawn_repro, "run_batch",
        _stub_matrix({"start_new_session", "parent_threads"}),
    )
    assert spawn_repro.main([
        "--matrix", "--assert-single-cause", "--json-out", str(out),
    ]) == 1


# ---------------------------------------------------------------------------
# 4. The targets
# ---------------------------------------------------------------------------


def test_resolve_target_builds_each_target(tmp_path):
    """Each target names its own command, working directory and wiring."""
    assert spawn_repro.TARGETS == ("tiny", "pdt")

    tiny = spawn_repro.resolve_target("tiny", 12345, tmp_path)
    assert "tiny_service.py" in tiny.start_cmd
    assert "--port 12345" in tiny.start_cmd
    assert tiny.cwd == _BACKEND_DIR
    assert tiny.port == 12345

    pdt = spawn_repro.resolve_target("pdt", 12345, tmp_path)
    assert "-m backend.server" in pdt.start_cmd
    assert pdt.cwd == _BACKEND_DIR.parent
    assert pdt.env_extra["PDT_PORT"] == "12345"
    assert pdt.env_extra["PDT_PLANS_DIR"].startswith(str(tmp_path))
    assert pdt.env_extra["PDT_STATE_DB_PATH"].startswith(str(tmp_path))


def test_unknown_target_is_refused(tmp_path):
    """A target nobody defined is an error, not a silent default."""
    with pytest.raises(ValueError):
        spawn_repro.resolve_target("nginx", 12345, tmp_path)


def test_usage_error_exits_two():
    """A wrong command line exits 2 -- not 0, and not 1."""
    assert spawn_repro.main(["--target", "nginx"]) == 2
    assert spawn_repro.main(["--assert-single-cause"]) == 2


# ---------------------------------------------------------------------------
# 5. The tiny service imports nothing that listens
# ---------------------------------------------------------------------------


def test_tiny_service_import_has_no_side_effect(monkeypatch):
    """Importing the tiny service must not start a server.

    Two spies, because "no side effect" has two halves: the module must
    not run a server, and it must not leave a socket listening.
    """
    listened = []
    original_listen = socket.socket.listen

    def _spy_listen(self, *args, **kwargs):
        listened.append(self.getsockname())
        return original_listen(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "listen", _spy_listen, raising=False)

    import uvicorn

    def _boom(*args, **kwargs):
        raise AssertionError("importing tiny_service must not start a server")

    monkeypatch.setattr(uvicorn, "run", _boom)

    module = _load(_TINY_PATH, "tiny_service_under_test")

    assert module is not None
    assert listened == [], "importing tiny_service opened a listening socket"
