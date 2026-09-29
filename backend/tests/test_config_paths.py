"""Contract tests for :mod:`backend.config_paths`.

``config_paths`` is the single source of truth for every filesystem
path the backend derives from ``__file__``. Before it existed each
leaf module re-derived ``Path(__file__).parent.parent / "plans"`` (and
friends) on its own, and ``provider_order.py`` went as far as importing
``server.PROVIDER_ORDER_FILE`` — an upward dependency from a leaf
utility onto the FastAPI app module (architecture review finding #2).

The tests below pin the two properties that make the module usable as
that source of truth:

1. **Correct anchoring** — every constant hangs off ``PROJECT_ROOT`` /
   ``BACKEND_DIR`` rather than the current working directory.
2. **Import-safety** — importing it must not pull in ``server`` (or
   FastAPI), and must not touch the filesystem.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config_paths


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def test_backend_dir_and_project_root_anchor_the_repo():
    assert config_paths.BACKEND_DIR.is_absolute()
    assert config_paths.BACKEND_DIR.name == "backend"
    assert config_paths.PROJECT_ROOT == config_paths.BACKEND_DIR.parent
    # Sanity: the anchors really point at this checkout.
    assert (config_paths.BACKEND_DIR / "server.py").exists()


def test_repo_level_paths_hang_off_project_root():
    root = config_paths.PROJECT_ROOT
    assert config_paths.PLANS_DIR == root / "plans"
    assert config_paths.FRONTEND_DIR == root / "frontend"
    assert config_paths.TOOLS_DIR == root / "tools"
    # Runtime state is the one repo-level path that is NOT a bare
    # ``root / "<name>"``: since 2026-09-28 the database, its WAL
    # siblings, the boot counter and the snapshot dir live together under
    # ``.pdt/``. It still hangs off PROJECT_ROOT — it is not an
    # out-of-tree location — but asserting the old literal would now
    # encode the layout that was deliberately replaced.
    assert config_paths.STATE_DIR == root / ".pdt"
    assert config_paths.STATE_DB == config_paths.STATE_DIR / "state.db"


def test_backend_level_paths_hang_off_backend_dir():
    backend = config_paths.BACKEND_DIR
    assert config_paths.CONFIGS_DIR == backend / "configs"
    assert config_paths.BACKEND_CONFIG_YAML == backend / "config.yaml"
    assert config_paths.ENV_FILE == backend / ".env"


@pytest.mark.parametrize(
    "attr",
    [
        "CONFIGS_DIR",
        "BASE_CONFIG_YAML",
        "VERIFICATION_CONFIG_YAML",
        "CODING_CONFIG_YAML",
        "ARCH_PRINCIPLES_YAML",
        "BACKEND_CONFIG_YAML",
        "FRONTEND_DIR",
    ],
)
def test_declared_config_paths_exist_in_this_checkout(attr):
    """These are checked-in files/dirs — a typo would silently break
    every consumer, so assert they resolve to something real."""
    assert getattr(config_paths, attr).exists(), f"{attr} does not exist"


# ---------------------------------------------------------------------------
# provider-order.json resolution (architecture review finding #2)
# ---------------------------------------------------------------------------


def test_default_provider_order_file_lives_in_config_dir():
    """The default is an operator runtime artifact, not a shipped file.

    It sits beside the other per-deployment configuration: whichever
    producer an installation runs writes it there, and nothing has to
    exist in a clean clone for the server to start.
    """
    assert (
        config_paths.DEFAULT_PROVIDER_ORDER_FILE
        == config_paths.CONFIG_DIR / "provider-order.json"
    )


def test_resolve_provider_order_file_prefers_env_var(monkeypatch, tmp_path):
    override = tmp_path / "custom-order.json"
    monkeypatch.setenv("PROVIDER_ORDER_FILE", str(override))
    assert config_paths.resolve_provider_order_file() == override


def test_resolve_provider_order_file_resolves_relative_env_against_cwd(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROVIDER_ORDER_FILE", "state/order.json")
    resolved = config_paths.resolve_provider_order_file()
    assert resolved.is_absolute()
    assert resolved == (Path(os.getcwd()) / "state" / "order.json").resolve()


def test_resolve_provider_order_file_falls_back_to_config_yaml(monkeypatch):
    """With no env override the value comes from backend/config.yaml,
    resolved against PROJECT_ROOT (not the CWD)."""
    monkeypatch.delenv("PROVIDER_ORDER_FILE", raising=False)
    resolved = config_paths.resolve_provider_order_file()
    assert resolved.is_absolute()
    assert resolved == config_paths.DEFAULT_PROVIDER_ORDER_FILE.resolve()


def test_resolve_provider_order_file_uses_hardcoded_default_when_yaml_missing(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("PROVIDER_ORDER_FILE", raising=False)
    monkeypatch.setattr(
        config_paths, "BACKEND_CONFIG_YAML", tmp_path / "does-not-exist.yaml"
    )
    assert (
        config_paths.resolve_provider_order_file()
        == config_paths.DEFAULT_PROVIDER_ORDER_FILE
    )


def test_provider_order_file_constant_is_resolved_at_import():
    assert isinstance(config_paths.PROVIDER_ORDER_FILE, Path)
    assert config_paths.PROVIDER_ORDER_FILE.is_absolute()


# ---------------------------------------------------------------------------
# Import-safety
# ---------------------------------------------------------------------------


def test_importing_config_paths_does_not_import_server():
    """A leaf utility must be able to import path constants without
    dragging in server.py (and therefore FastAPI app construction)."""
    code = (
        "import sys; import config_paths; "
        "print(int('server' in sys.modules), int('fastapi' in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(config_paths.BACKEND_DIR),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0 0", (
        f"config_paths pulled in server/fastapi: {proc.stdout!r}"
    )


_MUTATING_CALLS = frozenset(
    {"mkdir", "touch", "write_text", "write_bytes", "unlink", "rmdir", "rename"}
)


def test_module_declares_paths_without_mutating_the_filesystem():
    """The module declares paths; it never creates or removes them.

    Checked against the parsed AST rather than the raw text so that
    prose in docstrings (which legitimately mentions ``mkdir``) does
    not trip the assertion.
    """
    tree = ast.parse(Path(config_paths.__file__).read_text(encoding="utf-8"))
    offenders = [
        f"{node.func.attr}() at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATING_CALLS
    ]
    assert not offenders, f"config_paths mutates the filesystem: {offenders}"
